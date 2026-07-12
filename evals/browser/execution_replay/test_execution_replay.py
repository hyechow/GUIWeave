"""Offline replay of execution fixtures.

Version 2 fixtures preserve recorded turn payloads and adapter observations from a live run.
Version 1 remains readable while older distilled fixtures are migrated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.core.run.execution_signals import (
    CompletionEvaluation,
    CompletionEvaluator,
    ConstraintLedger,
    EvidenceClaim,
    ExecutionContract,
)
from gui_agent.core.run.flow import evaluate_turn_progress
from gui_agent.core.run.loop import _needs_terminal_reconciliation
from gui_agent.core.run.turns import (
    interactive_turn_count,
    make_interactive_turn,
    make_verdict_turn,
)
from gui_agent.core.schemas import (
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyContext,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone import policy as policy_module
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.action_protocol import record_action_outcome
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
from gui_agent.adapters.browser.control_grounding import (
    ground_rendered_action,
    rendered_target_evidence,
    resolve_native_control_action,
)
from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.adapters.browser.policies import BrowserActionPolicy
from gui_agent.adapters.browser.target_binding import BrowserTargetBinder
from gui_agent.core.run.target_binding import bind_action_target
from gui_agent.adapters.browser.supervisor.milestone.prompts import BrowserPlanResult
from gui_agent.core.supervisor.milestone.feasibility import semantic_target_present


TRACES = tuple(sorted(Path(__file__).parent.glob("trace_*.json")))


def _completion_case(case: dict[str, Any]) -> None:
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone.model_validate(case["milestone"])
    observation = Observation.model_validate(case["observation"])
    history = [PolicyTurn.model_validate(item) for item in case["history"]]
    check = _SingleCheckResult.model_validate(case["check"])

    decision = policy._completion_decision_from_check(
        milestone,
        observation,
        history,
        check,
    )
    expected = case["expected"]
    expected_status = {
        "complete": "satisfied",
        "continue": "pending",
        "replan": "contradicted",
        "delegate": "delegated",
    }[expected["action"]]
    assert decision.status == expected_status
    assert decision.completion_status == expected["completion_status"]
    if "conflicts" in expected:
        assert list(decision.conflicts) == expected["conflicts"]


def _filter_completion_case(case: dict[str, Any]) -> None:
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone.model_validate(case["milestone"])
    observation = Observation.model_validate(case["observation"])
    history = [PolicyTurn.model_validate(item) for item in case.get("history", [])]
    policy.reseed(milestone)

    original = policy_module.is_loading_frame
    policy_module.is_loading_frame = lambda _observation: False
    try:
        step = policy._run_single_turn(milestone, observation, history)
    finally:
        policy_module.is_loading_frame = original

    expected = case["expected"]
    assert step.goal_completed is expected["goal_completed"]
    assert step.completion_status == expected["completion_status"]
    assert milestone.status == expected["milestone_status"]
    if "pre_existing" in expected:
        assert step.pre_existing is expected["pre_existing"]


def _scope_case(case: dict[str, Any]) -> None:
    ledger = ConstraintLedger()
    for entry in case["entries"]:
        ledger.add(entry["text"], scope=entry["scope"], source=entry["source"])
    for scope, expected in case["expected"].items():
        assert ledger.visible(scope) == expected


def _suppression_progress_case(case: dict[str, Any]) -> None:
    step = SupervisorStep.model_validate(case["step"])
    action_decision = BaseActionDecision.model_validate(case["action_decision"])
    common = {
        "prev_milestone_id": step.milestone_id,
        "sv_step": step,
        "executed": False,
        "action_decision": action_decision,
        "probe_failed": False,
        "suppressed_reason": case["suppressed_reason"],
    }
    first = evaluate_turn_progress(noop_count=0, **common)
    third = evaluate_turn_progress(noop_count=2, **common)
    expected = case["expected"]
    assert first.message == expected["first_message"]
    assert third.stop_reason == expected["third_stop_reason"]


def _lifecycle_monotonic_case(case: dict[str, Any]) -> None:
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone.model_validate(case["milestone"])
    dispatched = PolicyTurn.model_validate(case["dispatched_turn"])
    contradiction = _SingleCheckResult.model_validate(case["contradiction_check"])
    later_unverified = _SingleCheckResult.model_validate(case["later_unverified_check"])

    record_action_outcome(
        [dispatched],
        milestone,
        _fused_result(contradiction),
        ledger=policy._action_ledger,
    )
    record_action_outcome(
        [dispatched],
        milestone,
        _fused_result(later_unverified),
        ledger=policy._action_ledger,
    )
    signal = dispatched.action_signal
    assert signal is not None
    expected = case["expected"]
    assert signal.outcome == expected["preserved_outcome"]


def _checker_feedback_case(case: dict[str, Any]) -> None:
    check = _SingleCheckResult.model_validate(case["check"])
    claim = MilestoneSupervisorPolicy._checker_claim(  # noqa: SLF001
        check, scope=case["scope"]
    )
    assert claim.value == case["expected"]["claim_value"]


def _lifecycle_closed_case(case: dict[str, Any]) -> None:
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone.model_validate(case["milestone"])
    dispatched = PolicyTurn.model_validate(case["dispatched_turn"])
    for payload in case["checks"]:
        record_action_outcome(
            [dispatched],
            milestone,
            _fused_result(_SingleCheckResult.model_validate(payload)),
            ledger=policy._action_ledger,
        )
    signal = dispatched.action_signal
    assert signal is not None
    assert signal.outcome == case["expected"]["outcome"]


def _fused_result(check: _SingleCheckResult) -> CompletionEvaluation:
    if check.outcome_status == "contradicted":
        return CompletionEvaluation(status="contradicted", reason=check.reason)
    if check.outcome_status == "confirmed":
        return CompletionEvaluation(
            status="satisfied",
            reason=check.reason,
            completion_status="confirmed",
        )
    return CompletionEvaluation(status="pending", reason=check.reason)


def _native_action_case(case: dict[str, Any]) -> None:
    decision = resolve_native_control_action(
        case["form_controls"],
        target_control=case["target_control"],
        target_value=case["target_value"],
        target_group_id=case["target_group_id"],
        action_family=case["action_family"],
        instruction=case["instruction"],
    )
    assert decision is not None
    expected = case["expected"]
    assert decision.action.action_type == expected["action_type"]
    assert decision.action.text == expected["text"]
    assert round(float(decision.action.x), 3) == expected["x"]
    assert round(float(decision.action.y), 3) == expected["y"]
    assert decision.action.snap["method"] == expected["resolution_method"]
    assert decision.action.snap["info"] == expected["target_info"]


def _legacy_control_grounding_case(case: dict[str, Any]) -> None:
    """Replay coordinate correction without bypassing the visual action policy."""
    controls = case["form_controls"]
    source_decision = BrowserActionDecision.model_validate(case["action_decision"])
    source_action = source_decision.action
    decision = ground_rendered_action(
        source_decision,
        controls,
        target_control=case["target_control"],
        target_value=str(source_action.text or ""),
        target_group_id=str(
            case.get("target_group_id")
            or controls[0].get("group_id")
            or "__form__"
        ),
        action_family=case["action_family"],
    )
    expected = case["expected"]
    assert round(float(decision.action.x), 3) == expected["x"]
    assert round(float(decision.action.y), 3) == expected["y"]
    assert decision.action.snap["method"] == expected["grounding_method"]
    assert decision.action.snap["info"] == expected["target_info"]


def _browser_plan_schema_case(case: dict[str, Any]) -> None:
    plan = BrowserPlanResult.model_validate(case["plan"])
    expected = case["expected"]
    assert plan.atomic_role == expected["atomic_role"]
    assert plan.action_family == expected["action_family"]


def _browser_action_postprocess_case(case: dict[str, Any]) -> None:
    decision = BrowserActionDecision.model_validate(case["action_decision"])
    result = BrowserActionPolicy()._postprocess(decision, case["instruction"])
    expected = case["expected"]
    assert result.action.action_type == expected["action_type"]
    assert result.action.text == expected.get("text")
    if "description" in expected:
        assert result.action.description == expected["description"]


def _semantic_target_presence_case(case: dict[str, Any]) -> None:
    assert semantic_target_present(case["semantic_tree"], case["targets"]) is case["expected"]["present"]


def _signal_fusion_case(case: dict[str, Any]) -> None:
    scope = case["scope"]
    contract = ExecutionContract(**case["contract"])
    claims = []
    for payload in case["claims"]:
        item = dict(payload)
        authoritative = bool(item.pop("authoritative", False))
        claims.append(EvidenceClaim(
            scope=scope,
            authoritative_for=((item["domain"],) if authoritative else ()),
            **item,
        ))
    decision = CompletionEvaluator().decide(contract, claims, scope=scope)
    expected_status = {
        "complete": "satisfied",
        "continue": "pending",
        "replan": "contradicted",
        "delegate": "delegated",
    }[case["expected"]["action"]]
    assert decision.status == expected_status
    assert decision.completion_status == case["expected"]["completion_status"]
    if "conflicts" in case["expected"]:
        assert list(decision.conflicts) == case["expected"]["conflicts"]


def _target_binding_case(case: dict[str, Any]) -> None:
    recorded = case.get("recorded_turn") or {}
    outcome = bind_action_target(
        binder=BrowserTargetBinder(),
        step=SupervisorStep.model_validate(recorded.get("supervisor") or case["step"]),
        observation=Observation.model_validate(case["observation"]),
        action_decision=BrowserActionDecision.model_validate(
            recorded.get("action_decision") or case["action_decision"]
        ),
    )
    expected = case["expected"]
    assert outcome.status == expected["status"]
    if "source" in expected:
        assert outcome.source == expected["source"]


def _rendered_target_evidence_case(case: dict[str, Any]) -> None:
    evidence = rendered_target_evidence(
        case["form_controls"],
        target_control=case["target_control"],
        target_value=case["target_value"],
        target_group_id=case["target_group_id"],
        action_family=case["action_family"],
    )
    for text in case["expected"]["contains"]:
        assert text in evidence
    for text in case["expected"].get("not_contains", []):
        assert text not in evidence


def _terminal_reconcile_case(case: dict[str, Any]) -> None:
    milestone = Milestone.model_validate(case["milestone"])
    observation = Observation.model_validate(case["observation"])
    history = [PolicyTurn.model_validate(item) for item in case["history"]]
    policy = MilestoneSupervisorPolicy()
    policy.reseed(milestone)
    policy._monitor._last_dom_state = case["previous_dom_state"]  # noqa: SLF001
    if case.get("previous_url"):
        policy._monitor._last_url = case["previous_url"]  # noqa: SLF001
    policy._single_check = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: _SingleCheckResult.model_validate(case["check"])
    )
    policy._invoke_planner = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal reconciliation invoked planner")
        )
    )
    context = PolicyContext(
        goal="replay",
        supervisor_policy_name="milestone",
        action_policy_name="action",
        turns=history,
    )

    assert _needs_terminal_reconciliation(context) is True
    original = policy_module.is_loading_frame
    policy_module.is_loading_frame = lambda _observation: False
    try:
        step = policy.reconcile(observation, "replay", history)
    finally:
        policy_module.is_loading_frame = original
    context.turns.append(make_verdict_turn(
        index=len(context.turns) + 1,
        observation_source=observation.source,
        supervisor_step=step,
        supervisor=policy,
        observation_only=True,
    ))

    signal = history[-1].action_signal
    assert signal is not None
    expected = case["expected"]
    assert step.should_act is False
    assert signal.execution == "dispatched"
    assert signal.response == expected["response"]
    assert signal.outcome == expected["outcome"]
    if "goal_completed" in expected:
        assert step.goal_completed is expected["goal_completed"]
    if "completion_status" in expected:
        assert step.completion_status == expected["completion_status"]
    assert interactive_turn_count(context) == len(history)
    assert _needs_terminal_reconciliation(context) is False


def run_replay() -> list[str]:
    failures: list[str] = []
    handlers = {
        "completion": _completion_case,
        "filter_completion": _filter_completion_case,
        "scope_isolation": _scope_case,
        "suppression_progress": _suppression_progress_case,
        "lifecycle_monotonic": _lifecycle_monotonic_case,
        "checker_feedback": _checker_feedback_case,
        "lifecycle_closed": _lifecycle_closed_case,
        "native_action": _native_action_case,
        "control_grounding": _legacy_control_grounding_case,
        "browser_plan_schema": _browser_plan_schema_case,
        "browser_action_postprocess": _browser_action_postprocess_case,
        "semantic_target_presence": _semantic_target_presence_case,
        "signal_fusion": _signal_fusion_case,
        "target_binding": _target_binding_case,
        "rendered_target_evidence": _rendered_target_evidence_case,
        "terminal_reconcile": _terminal_reconcile_case,
    }
    retired_gate_cases = {
        "proposal",
        "dispatch",
        "target_unit",
        "duplicate_write_dispatch",
    }
    for trace_path in TRACES:
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        assert trace["version"] in {1, 2}
        known_turns = trace["source"].get("turns", {})
        if trace["version"] == 1:
            assert all(len(digest) == 40 for digest in known_turns.values())
        for case in trace["cases"]:
            try:
                if trace["version"] == 1:
                    for turn in case["source_turns"]:
                        assert str(turn) in known_turns
                else:
                    assert case["recorded_turn"]["index"] > 0
                if case["kind"] in retired_gate_cases:
                    continue
                handlers[case["kind"]](case)
            except Exception as exc:
                failures.append(f"{case['id']}: {type(exc).__name__}: {exc}")
    return failures


def test_execution_replay() -> None:
    assert run_replay() == []


def main() -> int:
    failures = run_replay()
    if failures:
        print("\n".join(failures))
        return 1
    case_count = sum(
        len(json.loads(path.read_text(encoding="utf-8"))["cases"])
        for path in TRACES
    )
    print(f"execution replay: {case_count} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
