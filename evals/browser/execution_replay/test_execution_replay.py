"""Offline replay of critical execution timing from a real browser run.

The fixture contains only distilled structural observations and recorded policy outputs. This
module calls production proposal, completion, dispatch, and scope machinery without a browser,
LLM, site knowledge, or evaluator oracle.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.core.run.action_exec import ActionExecutionState
from gui_agent.core.run.execution_signals import ConstraintLedger
from gui_agent.core.run.flow import evaluate_turn_progress
from gui_agent.core.schemas import (
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone import policy as policy_module
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


TRACES = tuple(sorted(Path(__file__).parent.glob("trace_*.json")))


class _Future:
    def result(self) -> None:
        return None


class _Executor:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def execute(self, action_decision, **_kwargs) -> bool:
        self.calls.append(action_decision)
        return True


def _proposal_case(case: dict[str, Any]) -> None:
    milestone = Milestone.model_validate(case["milestone"])
    observation = Observation.model_validate(case["observation"])
    check = _SingleCheckResult.model_validate(case["check"])
    history = [PolicyTurn.model_validate(item) for item in case.get("history", [])]
    plans = iter(
        _PlanResult.model_validate(item["plan"])
        for item in case["planner_outputs"]
    )
    planner_calls: list[str] = []
    policy = MilestoneSupervisorPolicy()

    def invoke(*_args, **kwargs):
        planner_calls.append(str(kwargs.get("extra") or ""))
        return next(plans)

    policy._invoke_planner = invoke  # type: ignore[method-assign]
    policy._is_repeated_instruction = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    step = policy._plan_single(milestone, check, observation, history)
    expected = case["expected"]
    assert len(planner_calls) == expected["planner_calls"]
    assert step.should_act is expected["should_act"]
    assert step.action_family == expected["action_family"]
    assert expected["instruction_contains"] in (step.instruction or "")


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
    assert decision.action == expected["action"]
    assert decision.completion_status == expected["completion_status"]


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


def _dispatch_case(case: dict[str, Any], temp_dir: Path) -> None:
    step = SupervisorStep.model_validate(case["step"])
    observation = Observation.model_validate(case["observation"])
    executor = _Executor()

    result = ActionExecutionState().run(
        sv_step=step,
        observation=observation,
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=object(),
        platform=object(),
        prep_future=_Future(),
        log_dir=temp_dir,
        turn_no=case["source_turns"][0],
        flash=lambda _action: None,
        status=lambda _turn, _message: None,
        say=lambda _message: None,
    )
    expected = case["expected"]
    assert result.executed is expected["executed"]
    assert len(executor.calls) == expected["executor_calls"]
    assert expected["suppressed_reason_contains"] in result.suppressed_reason


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
    correction = PolicyTurn.model_validate(case["correction_turn"])
    contradiction = _SingleCheckResult.model_validate(case["contradiction_check"])
    later_unverified = _SingleCheckResult.model_validate(case["later_unverified_check"])
    candidate_step = SupervisorStep.model_validate(case["candidate_step"])
    candidate_action = BaseActionDecision.model_validate(case["candidate_action"])

    policy._update_latest_action_outcome(
        [dispatched], milestone, contradiction
    )
    policy._update_latest_action_outcome(
        [dispatched], milestone, later_unverified
    )
    signal = dispatched.action_signal
    assert signal is not None
    allowed, _key, reason = policy.authorize_action_dispatch(
        candidate_step,
        candidate_action,
        [dispatched, correction],
    )
    expected = case["expected"]
    assert signal.outcome == expected["preserved_outcome"]
    assert allowed is expected["allowed"]
    assert reason == expected["reason"]


def run_replay() -> list[str]:
    failures: list[str] = []
    handlers = {
        "proposal": _proposal_case,
        "completion": _completion_case,
        "filter_completion": _filter_completion_case,
        "scope_isolation": _scope_case,
        "suppression_progress": _suppression_progress_case,
        "lifecycle_monotonic": _lifecycle_monotonic_case,
    }
    with tempfile.TemporaryDirectory(prefix="execution-replay-") as tmp:
        temp_dir = Path(tmp)
        for trace_path in TRACES:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            assert trace["version"] == 1
            known_turns = trace["source"]["turns"]
            assert all(len(digest) == 40 for digest in known_turns.values())
            for case in trace["cases"]:
                try:
                    for turn in case["source_turns"]:
                        assert str(turn) in known_turns
                    if case["kind"] == "dispatch":
                        _dispatch_case(case, temp_dir)
                    else:
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
