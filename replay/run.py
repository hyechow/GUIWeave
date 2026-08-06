"""Replay one production Statement decision without dispatching an action.

The replay input is a normal run directory containing ``context.json``,
``screenshot_turn_N.png`` and ``observation_turn_N.json``.  The observation snapshot is written
by the runtime alongside each screenshot and preserves the structured adapter signals that a PNG
cannot represent. This script calls the real Statement Transition and can optionally ground its
semantic decision through the action policy; it never constructs an executor or sends an action
to a device/browser.

Examples:
    uv run python -m replay logs/.../20260713_090810 --turn 30
      (equivalently: uv run python replay/run.py logs/.../20260713_090810 --turn 30)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.run.context import load_observation_snapshot
from gui_agent.core.run.action_signals import effective_action_role
from gui_agent.core.run.target_binding import bind_action_target
from gui_agent.core.run.contracts import Interact
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.schemas import (
    PolicyContext,
    PolicyTurn,
    StatementContract,
    StatementOutcomeEvent,
)
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app


def _statement_for_turn(raw: dict[str, Any], turn: PolicyTurn) -> StatementContract:
    step = turn.supervisor
    if step is None or not step.statement_id:
        raise ValueError(f"turn {turn.index} has no statement supervisor decision")
    info: object = turn.statement
    if info is None:
        info = next(
            (
                candidate.get("statement")
                for candidate in (raw.get("journal") or {}).get("events", [])
                if candidate.get("statement_instance_id") == turn.statement_instance_id
                and candidate.get("statement") is not None
            ),
            None,
        )
    if info is None:
        raise ValueError(
            f"statement invocation {turn.statement_instance_id!r} has no StatementInfo"
        )
    if hasattr(info, "model_dump"):
        info = info.model_dump(mode="json", exclude={"executor"})
    elif isinstance(info, dict):
        info = {key: value for key, value in info.items() if key != "executor"}
    return StatementContract.model_validate(info)


def _statement_for_terminal_observation(
    raw: dict[str, Any],
    *,
    statement_id: str,
) -> StatementContract:
    """Recover a contract when the terminal observation intentionally has no running turn."""
    program_raw = (raw.get("orchestrator") or {}).get("program")
    if not isinstance(program_raw, dict):
        raise ValueError("terminal replay requires orchestrator.program in context.json")

    def find(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in items:
            if item.get("id") == statement_id or item.get("statement_id") == statement_id:
                return item
            for key in ("then", "otherwise", "body"):
                if isinstance(item.get(key), list) and (match := find(item[key])):
                    return match
        return None

    blocks = [program_raw.get("statements") or []]
    if match := next(filter(None, (find(block) for block in blocks)), None):
        statement = Interact.model_validate(match)
        # Reuse the production Interact -> StatementContract mapping so the replay
        # contract cannot drift from what the runtime executes (a manual copy here
        # would silently drop expected_state / interaction_intent).
        from gui_agent.core.run.contracts import StatementInvocation
        from gui_agent.core.run.interactive import contract_for_interact

        return contract_for_interact(
            StatementInvocation(statement=statement, inputs={}),
            0,
        )
    raise ValueError(f"statement {statement_id!r} is absent from orchestrator.program")


def _terminal_event_for_turn(
    raw: dict[str, Any],
    *,
    turn: int,
    statement_id: str = "",
) -> dict[str, Any] | None:
    """Resolve a terminal decision by Journal Turn, with legacy capture fallback."""
    screenshot = f"screenshot_turn_{turn}.png"
    fallback = None
    for event in reversed((raw.get("journal") or {}).get("events") or []):
        if event.get("event_type") != "statement_outcome":
            continue
        if statement_id and event.get("statement_id") != statement_id:
            continue
        if event.get("after_turn") == turn:
            return event
        if fallback is None and event.get("observation_url") == screenshot:
            fallback = event
    return fallback


def _load_snapshot(run_dir: Path, turn: int):
    path = run_dir / f"observation_turn_{turn}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"missing {path}; screenshots alone are not replay-safe because they omit DOM, "
            "filter, viewport and semantic-tree signals"
        )
    return load_observation_snapshot(path)


def _snapshot_turn(observation_url: str, fallback: int) -> int:
    match = re.fullmatch(
        r"observation_turn_(\d+)\.json|screenshot_turn_(\d+)\.(?:png|jpe?g)",
        Path(observation_url or "").name,
    )
    if match is None:
        return fallback
    return int(match.group(1) or match.group(2))


def _history_before_selected_event(
    context: PolicyContext,
    *,
    target_index: int,
    terminal_event: dict[str, Any] | None,
) -> list[Any]:
    """Restore the exact journal prefix preceding the replayed decision."""
    for position, event in enumerate(context.journal.events):
        if terminal_event is None:
            if isinstance(event, PolicyTurn) and event.index == target_index:
                return list(context.journal.events[:position])
            continue
        if (
            isinstance(event, StatementOutcomeEvent)
            and event.after_turn == terminal_event.get("after_turn")
            and event.statement_id == terminal_event.get("statement_id")
            and event.statement_instance_id
            == terminal_event.get("statement_instance_id")
        ):
            return list(context.journal.events[:position])
    raise ValueError(f"selected replay event for turn {target_index} is absent from journal")


def _configure_knowledge(supervisor: Any, context: PolicyContext) -> None:
    summary = context.knowledge or {}
    app_name = str(summary.get("app_name") or "")
    if not app_name:
        return
    include_skills = summary.get("profile") not in {None, "functional-only"}
    knowledge = load_knowledge_for_app(
        app_name,
        context.platform or "browser",
        include_skills=include_skills,
    )
    if knowledge:
        supervisor.set_app_knowledge(
            knowledge.navigation,
            app_name=knowledge.app_name,
            elements=knowledge.elements,
            sections=knowledge.sections,
            check=knowledge.check,
        )


def _expectation_failures(
    expectation: dict[str, Any], decision: Any, supervisor: Any
) -> list[str]:
    failures: list[str] = []
    outcome = decision.outcome
    intent = decision.action_intent
    transition_record = supervisor._last_transition_record or {}
    actuals = {
        "assessment_status": (
            transition_record.get("proposal", {})
            .get("assessment", {})
            .get("status")
        ),
        "should_act": intent is not None,
        "phase": outcome.phase if outcome is not None else "running",
        "verification": outcome.verification if outcome is not None else None,
        "atomic_role": intent.role if intent is not None else "prepare",
        "action_family": intent.family if intent is not None else "unknown",
        "target_control": intent.target_control if intent is not None else "",
        "expected_result": (
            transition_record.get("proposal", {})
            .get("action", {})
            .get("expected_result", "")
        ),
        "direction": intent.direction if intent is not None else None,
    }
    checks = (
        ("assessment_status", expectation.get("assessment_status")),
        ("should_act", expectation.get("should_act")),
        ("phase", expectation.get("phase")),
        ("verification", expectation.get("verification")),
        ("atomic_role", expectation.get("atomic_role")),
        ("action_family", expectation.get("action_family")),
        ("target_control", expectation.get("target_control")),
        ("expected_result", expectation.get("expected_result")),
        ("direction", expectation.get("direction")),
    )
    for field, expected in checks:
        actual = actuals[field]
        if expected is not None and actual != expected:
            failures.append(
                f"expected {field}={expected!r}, got {actual!r}"
            )
    rejected = expectation.get("reject_target_controls") or []
    if intent is not None and intent.target_control in rejected:
        failures.append(
            f"rejected target_control was proposed: {intent.target_control!r}"
        )
    if expectation.get("retry_count") is not None:
        failures.append(
            "retry_count expectation is invalid: the minimal Statement runtime retired "
            "mutable retry counters"
        )
    return failures


def _action_expectation_failures(
    expectation: dict[str, Any],
    action_decision: Any,
    receipt_role: str,
) -> list[str]:
    expected = expectation.get("action")
    action = action_decision.action
    failures: list[str] = []
    if expected and expected.get("action_type") and action.action_type != expected["action_type"]:
        failures.append(
            f"expected action_type={expected['action_type']!r}, got {action.action_type!r}"
        )
    for field in ("x", "y"):
        bounds = expected.get(f"{field}_range") if expected else None
        value = getattr(action, field, None)
        if bounds and (
            not isinstance(value, (int, float))
            or not float(bounds[0]) <= float(value) <= float(bounds[1])
        ):
            failures.append(f"expected {field} in {bounds!r}, got {value!r}")
    expected_receipt_role = expectation.get("receipt_role")
    if expected_receipt_role is not None and receipt_role != expected_receipt_role:
        failures.append(
            f"expected receipt_role={expected_receipt_role!r}, got {receipt_role!r}"
        )
    return failures


def _decide_action_without_dispatch(
    observation: Any,
    decision: Any,
    action_policy: Any,
) -> Any:
    intent = decision.action_intent
    if intent is None:
        raise ValueError("action replay requires ActionIntent")
    target_group_id = ""
    native = action_policy.resolve_native_action(
        observation,
        target_control=intent.target_control,
        target_value=intent.target_value,
        target_ref=intent.target_ref,
        target_group_id=target_group_id,
        action_family=intent.family,
        instruction=intent.instruction,
    )
    if native is not None:
        return native
    evidence = action_policy.action_evidence_context(
        observation,
        target_control=intent.target_control,
        target_value=intent.target_value,
        target_ref=intent.target_ref,
        target_group_id=target_group_id,
        action_family=intent.family,
    )
    proposed = action_policy.decide(
        observation,
        intent.instruction,
        direction=intent.direction,
        drag_column=intent.drag_column,
        drag_steps=intent.drag_steps,
        action_family=intent.family,
        target_control=intent.target_control,
        target_value=intent.target_value,
        expected_result=intent.expected_result,
        evidence_context=evidence,
        verbose=False,
    )
    return action_policy.ground_rendered_action(
        proposed,
        observation,
        target_control=intent.target_control,
        target_value=intent.target_value,
        target_ref=intent.target_ref,
        target_group_id=target_group_id,
        action_family=intent.family,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay one real supervisor decision without executing its action."
    )
    parser.add_argument("run_dir", type=Path, help="run or promoted-fixture directory")
    parser.add_argument(
        "--turn",
        type=int,
        help="Journal Turn; defaults to replay_expectation.json",
    )
    parser.add_argument(
        "--statement-id",
        help=(
            "replay this StatementOutcome after the selected Journal Turn even when that "
            "Turn also recorded another statement's action"
        ),
    )
    parser.add_argument("--expect-role", choices=("prepare", "write", "commit", "iterate"))
    parser.add_argument(
        "--expect-family",
        choices=("input", "select", "activate", "navigate", "iterate", "unknown"),
    )
    parser.add_argument("--expect-target", help="require this exact target_control")
    parser.add_argument("--reject-target", help="fail if this exact target_control is proposed")
    parser.add_argument(
        "--with-action-policy",
        action="store_true",
        help="also generate the concrete action primitive without dispatching it",
    )
    parser.add_argument(
        "--expect-binding",
        choices=("bound", "contradicted", "unresolved"),
        help="also replay the pre-dispatch target binding and require this verdict",
    )
    parser.add_argument(
        "--expectation",
        type=Path,
        help="expectation JSON; defaults to <run_dir>/replay_expectation.json when present",
    )
    parser.add_argument("--expect-json", help="inline expectation JSON (used by suites)")
    parser.add_argument("--json", type=Path, help="write the compact replay result to this path")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    expectation_path = args.expectation
    if expectation_path is None:
        candidate = run_dir / "replay_expectation.json"
        expectation_path = candidate if candidate.is_file() else None
    expectation: dict[str, Any] = {}
    if expectation_path is not None:
        expectation = json.loads(expectation_path.read_text(encoding="utf-8"))
    if args.expect_json:
        expectation.update(json.loads(args.expect_json))
    if args.expect_role is not None:
        expectation["atomic_role"] = args.expect_role
    if args.expect_family is not None:
        expectation["action_family"] = args.expect_family
    if args.expect_target is not None:
        expectation["target_control"] = args.expect_target
    if args.reject_target is not None:
        expectation["reject_target_controls"] = [args.reject_target]
    if args.expect_binding is not None:
        expectation["binding_status"] = args.expect_binding
    target_index = args.turn or expectation.get("turn")
    if not target_index:
        parser.error("--turn is required when no replay expectation supplies it")

    raw = json.loads((run_dir / "context.json").read_text(encoding="utf-8"))
    context = PolicyContext.model_validate(raw)
    target_turn = next((turn for turn in context.journal.turns if turn.index == target_index), None)
    warnings: list[str] = []
    terminal_event = (
        _terminal_event_for_turn(
            raw,
            turn=target_index,
            statement_id=args.statement_id,
        )
        if args.statement_id
        else None
    )
    if target_turn is not None and terminal_event is None:
        statement_instance_id = target_turn.statement_instance_id
        statement = _statement_for_turn(raw, target_turn)
        observation_turn = _snapshot_turn(target_turn.observation_url or "", target_index)
    else:
        terminal_event = terminal_event or _terminal_event_for_turn(
            raw,
            turn=target_index,
        )
        if terminal_event is None:
            raise ValueError(
                f"turn {target_index} and its terminal event are absent from "
                f"{run_dir / 'context.json'}"
            )
        statement_instance_id = str(terminal_event.get("statement_instance_id") or "")
        statement_id = str(terminal_event.get("statement_id") or "")
        if not statement_instance_id or not statement_id:
            raise ValueError(f"terminal event after turn {target_index} lacks statement identity")
        statement_info = terminal_event.get("statement")
        if not isinstance(statement_info, dict):
            statement_info = next(
                (
                    event.get("statement")
                    for event in raw.get("journal", {}).get("events", [])
                    if event.get("statement_instance_id") == statement_instance_id
                    and isinstance(event.get("statement"), dict)
                ),
                None,
            )
        if isinstance(statement_info, dict):
            statement_info = {
                key: value
                for key, value in statement_info.items()
                if key != "executor"
            }
        statement = (
            StatementContract.model_validate(statement_info)
            if isinstance(statement_info, dict)
            else _statement_for_terminal_observation(raw, statement_id=statement_id)
        )
        warnings.append(
            "replaying the selected StatementOutcome on this observation; statement identity "
            "was recovered from the terminal event"
        )
        observation_turn = _snapshot_turn(
            str(terminal_event.get("observation_url") or ""),
            target_index,
        )
    observation = _load_snapshot(run_dir, observation_turn)
    observation_asset = f"observation_turn_{observation_turn}.json"
    history_events = _history_before_selected_event(
        context,
        target_index=target_index,
        terminal_event=terminal_event,
    )
    history = [event for event in history_events if isinstance(event, PolicyTurn)]
    platform_name = context.platform or "browser"
    bundle = build_platform(platform_name)
    supervisor = bundle.make_supervisor(context.supervisor_policy_name)
    _configure_knowledge(supervisor, context)
    supervisor._goal = context.goal
    invocation_history = [
        turn
        for turn in history
        if turn.statement_instance_id == statement_instance_id
    ]
    if invocation_history:
        supervisor.resume_statement(
            statement,
            instance_id=statement_instance_id,
            history=invocation_history,
        )
    else:
        supervisor.begin_statement(
            statement,
            instance_id=statement_instance_id,
            task_type=context.task_type or "action",
        )
    statement = supervisor._active_statement

    decision = supervisor.step(observation, context.goal, history_events)
    checker = getattr(supervisor, "_last_check", None)
    result = {
        "source": str(run_dir),
        "turn": target_index,
        "observation_asset": observation_asset,
        "history_turns": len(history),
        "statement": statement.model_dump(mode="json", exclude_none=True),
        "observation": {
            "url": observation.url,
            "title": observation.title,
            "dom_state": observation.dom_state,
            "form_controls_meta": observation.form_controls_meta,
        },
        "checker": checker.model_dump(mode="json") if checker is not None else None,
        "decision": decision.model_dump(
            mode="json",
            exclude_none=True,
            exclude={"outcome": {"observation"}},
        ),
        "warnings": warnings,
    }
    failures = _expectation_failures(expectation, decision, supervisor)
    action_decision = None
    target_binding = None
    receipt_role = None
    if args.with_action_policy or expectation.get("action") or expectation.get("binding_status"):
        if decision.action_intent is None:
            failures.append("action policy requested but supervisor returned no action")
        else:
            action_policy = bundle.make_action_policy(context.action_policy_name)
            action_decision = _decide_action_without_dispatch(
                observation,
                decision,
                action_policy,
            )
            receipt_role = effective_action_role(
                decision,
                action_decision.action,
                observation,
            )
            failures.extend(
                _action_expectation_failures(
                    expectation,
                    action_decision,
                    receipt_role,
                )
            )
            # Replay the structural target binding too. It runs pre-dispatch (it decides
            # whether a write would be suppressed), so it is replay-safe, and it is the gate
            # whose verdict this harness otherwise could not observe.
            expected_binding = expectation.get("binding_status")
            if platform_name == "browser":
                from gui_agent.adapters.browser.target_binding import BrowserTargetBinder

                target_binding = bind_action_target(
                    binder=BrowserTargetBinder(),
                    step=decision,
                    observation=observation,
                    action_decision=action_decision,
                )
                if expected_binding and target_binding.status != expected_binding:
                    failures.append(
                        f"expected binding_status={expected_binding!r}, "
                        f"got {target_binding.status!r}: {target_binding.reason}"
                    )
            elif expected_binding:
                failures.append(
                    f"binding replay is unavailable for platform {platform_name!r}"
                )
    result["action_decision"] = (
        action_decision.model_dump(mode="json", exclude_none=True)
        if action_decision is not None
        else None
    )
    result["target_binding"] = (
        target_binding.model_dump(mode="json") if target_binding is not None else None
    )
    result["receipt_role"] = receipt_role
    result["expectation_failures"] = failures

    print(
        json.dumps(
            {
                "turn": target_index,
                "observation_asset": observation_asset,
                "checker_status": getattr(checker, "status", None),
                "checker_effect": getattr(checker, "effect_status", None),
                "assessment": (
                    (supervisor._last_transition_record or {})
                    .get("proposal", {})
                    .get("assessment")
                ),
                "validation_error": (supervisor._last_transition_record or {}).get(
                    "validation_error", ""
                ),
                "instruction": (
                    decision.action_intent.instruction
                    if decision.action_intent is not None
                    else None
                ),
                "should_act": decision.action_intent is not None,
                "outcome": (
                    decision.outcome.model_dump(
                        mode="json",
                        exclude={"observation"},
                    )
                    if decision.outcome
                    else None
                ),
                "atomic_role": (
                    decision.action_intent.role
                    if decision.action_intent is not None
                    else "prepare"
                ),
                "receipt_role": result["receipt_role"],
                "action_family": (
                    decision.action_intent.family
                    if decision.action_intent is not None
                    else "unknown"
                ),
                "target_control": (
                    decision.action_intent.target_control
                    if decision.action_intent is not None
                    else ""
                ),
                "action": result["action_decision"],
                "target_binding": (
                    target_binding.model_dump(mode="json") if target_binding is not None else None
                ),
                "warnings": warnings,
                "expectation_failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
