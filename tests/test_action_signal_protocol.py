from __future__ import annotations

import pytest

from gui_agent.core.orchestrator.program import Finish, Program, Run, RunResult
from gui_agent.core.orchestrator.runner import Interpreter, RunRecord, make_run_result
from gui_agent.core.run.action_ledger import semantic_action_key
from gui_agent.core.run.result import orchestration_result
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.schemas import (
    ActionSignal,
    BaseAction,
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyContext,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult


def _step(*, scope: str = "row:65", role: str = "commit") -> SupervisorStep:
    return SupervisorStep(
        should_act=True,
        instruction="点击 Save",
        stop=False,
        goal_completed=False,
        summary="",
        milestone_id="m1",
        execution_scope=scope,
        atomic_role=role,
    )


def _decision(x: float = 500) -> BaseActionDecision:
    return BaseActionDecision(
        action=BaseAction(action_type="tap", x=x, y=800, description="点击 Save")
    )


def _turn(
    *,
    index: int,
    step: SupervisorStep,
    outcome: str = "unverified",
    role: str | None = None,
) -> PolicyTurn:
    decision = _decision()
    signal_role = role or step.atomic_role
    key = semantic_action_key(step, decision.action)
    return PolicyTurn(
        index=index,
        observation_source="browser",
        supervisor=step,
        action_decision=decision,
        executed=True,
        action_signal=ActionSignal(
            action_key=key,
            role=signal_role,
            execution="dispatched",
            target="on_target",
            outcome=outcome,
        ),
    )


def test_commit_key_ignores_button_coordinate_within_same_resource():
    step = _step()

    assert semantic_action_key(step, _decision(400).action) == semantic_action_key(
        step, _decision(700).action
    )


def test_make_turn_records_execution_separately_from_outcome():
    turn = make_interactive_turn(
        index=1,
        observation_source="browser",
        supervisor_step=_step(),
        action_decision=_decision(),
        executed=True,
    )

    assert turn.action_signal is not None
    assert turn.action_signal.execution == "dispatched"
    assert turn.action_signal.response == "unknown"
    assert turn.action_signal.outcome == "unverified"


def test_duplicate_commit_is_suppressed_before_second_dispatch():
    policy = MilestoneSupervisorPolicy()
    step = _step()
    allowed, _key, reason = policy.authorize_action_dispatch(
        step, _decision(700), [_turn(index=1, step=step)]
    )

    assert allowed is False
    assert "禁止" in reason


def test_same_commit_template_is_allowed_for_different_foreach_row():
    policy = MilestoneSupervisorPolicy()
    first_step = _step(scope="row:65")
    second_step = _step(scope="row:66")

    allowed, _key, _reason = policy.authorize_action_dispatch(
        second_step, _decision(), [_turn(index=1, step=first_step)]
    )

    assert allowed is True


def test_contradicted_commit_requires_intervening_correction():
    policy = MilestoneSupervisorPolicy()
    commit = _step()
    rejected = _turn(index=1, step=commit, outcome="contradicted")

    denied, _key, _reason = policy.authorize_action_dispatch(commit, _decision(), [rejected])
    assert denied is False

    prepare = _step(role="prepare")
    corrected = _turn(index=2, step=prepare, role="prepare")
    allowed, _key, _reason = policy.authorize_action_dispatch(
        commit, _decision(), [rejected, corrected]
    )
    assert allowed is True


@pytest.mark.parametrize("checker_status", ["in_progress", "done"])
def test_terminal_dispatch_advances_as_accepted_unverified(
    monkeypatch, checker_status
):
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m1",
        name="保存记录",
        description="",
        success_condition="记录已保存",
        kind="action",
    )
    policy._milestones = {"m1": milestone}
    policy._current_id = "m1"
    policy._order = ["m1"]
    step = _step(scope="milestone:m1")
    history = [_turn(index=1, step=step)]
    monkeypatch.setattr(
        "gui_agent.core.supervisor.milestone.policy.is_loading_frame",
        lambda _obs: False,
    )
    monkeypatch.setattr(
        policy,
        "_single_check",
        lambda *a, **k: _SingleCheckResult(
            status=checker_status,
            reason="未看到成功提示",
            summary="缺少可见反馈",
            outcome_status="unverified",
        ),
    )

    result = policy._run_single_turn(
        milestone,
        Observation(png_bytes=b"x", source="browser", url="http://x/record/65"),
        history,
    )

    assert result.goal_completed is True
    assert result.completion_status == "accepted_unverified"
    assert milestone.completion_status == "accepted_unverified"


def test_accepted_unverified_run_result_advances_interpreter_but_is_not_verified():
    program = Program(
        statements=[
            Run(kind="action", name="保存记录"),
            Finish(message="已提交"),
        ]
    )
    interp = Interpreter(program)
    gen = interp.steps()
    run = next(gen)
    result = make_run_result(
        run,
        completed=True,
        completion_status="accepted_unverified",
        summary="动作已派发，结果未验证",
        notes=[],
    )

    try:
        gen.send(result)
    except StopIteration as exc:
        assert exc.value == "已提交"
    else:  # pragma: no cover - the finish must terminate the tiny program
        raise AssertionError("interpreter did not finish")

    assert result.completed is True
    assert result.failed is False
    assert result.verified is False
    assert interp.run_log[0].result.completion_status == "accepted_unverified"


def test_legacy_run_result_infers_confirmed_status():
    result = RunResult(completed=True)

    assert result.completion_status == "confirmed"
    assert result.verified is True


def test_final_result_does_not_report_unverified_dispatch_as_success():
    interp = Interpreter(Program(goal="保存记录", statements=[Finish(message="done")]))
    interp.run_log = [
        RunRecord(
            name="保存记录",
            result=RunResult(
                completed=True,
                completion_status="accepted_unverified",
                summary="动作已派发，结果未验证",
            ),
        )
    ]
    context = PolicyContext(
        goal="保存记录",
        supervisor_policy_name="milestone",
        action_policy_name="action",
    )

    result = orchestration_result(context, interp, "done", current=None)

    assert result["goal_completed"] is False
    assert result["goal_status"] == "accepted_unverified"
    assert result["orchestrator"]["accepted_unverified"] is True
