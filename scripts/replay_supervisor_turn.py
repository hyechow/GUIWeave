"""Replay one production supervisor turn without dispatching an action.

The replay input is a normal run directory containing ``context.json``,
``screenshot_turn_N.png`` and ``observation_turn_N.json``.  The observation snapshot is written
by the runtime alongside each screenshot and preserves the structured adapter signals that a PNG
cannot represent.  This script calls the real checker, selector and planner; it never constructs
an executor or sends an action to a device/browser.

Examples:
    uv run python scripts/replay_supervisor_turn.py logs/.../20260713_090810 --turn 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.adapters.browser.factory import _build_action_policy, _build_supervisor
from gui_agent.adapters.browser.target_binding import BrowserTargetBinder
from gui_agent.core.run.context import load_observation_snapshot
from gui_agent.core.run.target_binding import bind_action_target
from gui_agent.core.schemas import StatementContract, PolicyContext, PolicyTurn
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
        info = info.model_dump(mode="json")
    return StatementContract.model_validate(info)


def _load_snapshot(run_dir: Path, turn: int):
    path = run_dir / f"observation_turn_{turn}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"missing {path}; screenshots alone are not replay-safe because they omit DOM, "
            "filter, viewport and semantic-tree signals"
        )
    return load_observation_snapshot(path)


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
    actuals = {
        "should_act": decision.should_act,
        "phase": outcome.phase if outcome is not None else "running",
        "verification": outcome.verification if outcome is not None else None,
        "atomic_role": decision.atomic_role,
        "action_family": decision.action_family,
        "target_control": decision.target_control,
        "direction": decision.direction,
    }
    checks = (
        ("should_act", expectation.get("should_act")),
        ("phase", expectation.get("phase")),
        ("verification", expectation.get("verification")),
        ("atomic_role", expectation.get("atomic_role")),
        ("action_family", expectation.get("action_family")),
        ("target_control", expectation.get("target_control")),
        ("direction", expectation.get("direction")),
    )
    for field, expected in checks:
        actual = actuals[field]
        if expected is not None and actual != expected:
            failures.append(
                f"expected {field}={expected!r}, got {actual!r}"
            )
    rejected = expectation.get("reject_target_controls") or []
    if getattr(decision, "target_control", None) in rejected:
        failures.append(
            f"rejected target_control was proposed: {decision.target_control!r}"
        )
    expected_retries = expectation.get("retry_count")
    actual_retries = supervisor._rt.retry_count
    if expected_retries is not None and actual_retries != expected_retries:
        failures.append(
            f"expected retry_count={expected_retries!r}, got {actual_retries!r}"
        )
    return failures


def _action_expectation_failures(
    expectation: dict[str, Any],
    action_decision: Any,
) -> list[str]:
    expected = expectation.get("action")
    if not expected:
        return []
    action = action_decision.action
    failures: list[str] = []
    if expected.get("action_type") and action.action_type != expected["action_type"]:
        failures.append(
            f"expected action_type={expected['action_type']!r}, got {action.action_type!r}"
        )
    for field in ("x", "y"):
        bounds = expected.get(f"{field}_range")
        value = getattr(action, field, None)
        if bounds and (
            not isinstance(value, (int, float))
            or not float(bounds[0]) <= float(value) <= float(bounds[1])
        ):
            failures.append(f"expected {field} in {bounds!r}, got {value!r}")
    return failures


def _decide_action_without_dispatch(
    context: PolicyContext,
    observation: Any,
    decision: Any,
) -> Any:
    action_policy = _build_action_policy(context.action_policy_name)
    authorization = decision.mutation_authorization
    target_group_id = (
        authorization.subject_ref
        if authorization is not None and authorization.source == "structural"
        else ""
    )
    native = action_policy.resolve_native_action(
        observation,
        target_control=decision.target_control,
        target_value=decision.target_value,
        target_group_id=target_group_id,
        action_family=decision.action_family,
        instruction=decision.instruction or "",
    )
    if native is not None:
        return native
    evidence = action_policy.action_evidence_context(
        observation,
        target_control=decision.target_control,
        target_value=decision.target_value,
        target_group_id=target_group_id,
        action_family=decision.action_family,
    )
    proposed = action_policy.decide(
        observation,
        decision.instruction or "",
        direction=decision.direction,
        drag_column=decision.drag_column,
        drag_steps=decision.drag_steps,
        evidence_context=evidence,
        verbose=False,
    )
    return action_policy.ground_rendered_action(
        proposed,
        observation,
        target_control=decision.target_control,
        target_value=decision.target_value,
        target_group_id=target_group_id,
        action_family=decision.action_family,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay one real supervisor decision without executing its action."
    )
    parser.add_argument("run_dir", type=Path, help="run or promoted-fixture directory")
    parser.add_argument(
        "--turn",
        type=int,
        help="observation/decision turn; defaults to replay_expectation.json",
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
        choices=("bound", "unresolved", "contradicted"),
        help="also replay the pre-dispatch target binding and require this verdict",
    )
    parser.add_argument(
        "--expectation",
        type=Path,
        help="expectation JSON; defaults to <run_dir>/replay_expectation.json when present",
    )
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
    if target_turn is None:
        raise ValueError(f"turn {target_index} is absent from {run_dir / 'context.json'}")
    if (context.platform or "browser") != "browser":
        raise ValueError("this replay runner currently supports the browser supervisor only")

    observation = _load_snapshot(run_dir, target_index)
    history = [turn for turn in context.journal.turns if turn.index < target_index]
    statement = _statement_for_turn(raw, target_turn)
    supervisor = _build_supervisor(context.supervisor_policy_name)
    _configure_knowledge(supervisor, context)
    supervisor._goal = context.goal
    invocation_history = [
        turn
        for turn in history
        if turn.statement_instance_id == target_turn.statement_instance_id
    ]
    if invocation_history:
        supervisor.resume_statement(
            statement,
            instance_id=target_turn.statement_instance_id,
            history=invocation_history,
        )
    else:
        supervisor.begin_statement(
            statement,
            instance_id=target_turn.statement_instance_id,
            task_type=context.task_type or "action",
        )
    statement = supervisor._active_statement
    warnings: list[str] = []

    decision = supervisor.step(observation, context.goal, history)
    checker = getattr(supervisor, "_last_check", None)
    result = {
        "source": str(run_dir),
        "turn": target_index,
        "history_turns": len(history),
        "statement": statement.model_dump(mode="json", exclude_none=True),
        "observation": {
            "url": observation.url,
            "title": observation.title,
            "dom_state": observation.dom_state,
            "form_controls_meta": observation.form_controls_meta,
        },
        "checker": checker.model_dump(mode="json") if checker is not None else None,
        "decision": decision.model_dump(mode="json", exclude_none=True),
        "warnings": warnings,
    }
    failures = _expectation_failures(expectation, decision, supervisor)
    action_decision = None
    target_binding = None
    if args.with_action_policy or expectation.get("action") or expectation.get("binding_status"):
        if not decision.should_act or not decision.instruction:
            failures.append("action policy requested but supervisor returned no action")
        else:
            action_decision = _decide_action_without_dispatch(
                context,
                observation,
                decision,
            )
            failures.extend(
                _action_expectation_failures(expectation, action_decision)
            )
            # Replay the structural target binding too. It runs pre-dispatch (it decides
            # whether a write would be suppressed), so it is replay-safe, and it is the gate
            # whose verdict this harness otherwise could not observe.
            target_binding = bind_action_target(
                binder=BrowserTargetBinder(),
                step=decision,
                observation=observation,
                action_decision=action_decision,
            )
            expected_binding = expectation.get("binding_status")
            if expected_binding and target_binding.status != expected_binding:
                failures.append(
                    f"expected binding_status={expected_binding!r}, "
                    f"got {target_binding.status!r}: {target_binding.reason}"
                )
    result["action_decision"] = (
        action_decision.model_dump(mode="json", exclude_none=True)
        if action_decision is not None
        else None
    )
    result["target_binding"] = (
        target_binding.model_dump(mode="json") if target_binding is not None else None
    )
    result["expectation_failures"] = failures

    print(
        json.dumps(
            {
                "turn": target_index,
                "checker_status": getattr(checker, "status", None),
                "checker_effect": getattr(checker, "effect_status", None),
                "instruction": decision.instruction,
                "should_act": decision.should_act,
                "outcome": decision.outcome.model_dump(mode="json") if decision.outcome else None,
                "atomic_role": decision.atomic_role,
                "action_family": decision.action_family,
                "target_control": decision.target_control,
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
