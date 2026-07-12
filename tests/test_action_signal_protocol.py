from __future__ import annotations

import pytest

from gui_agent.core.orchestrator.program import Finish, Program, Run, RunResult
from gui_agent.core.orchestrator.runner import Interpreter, RunRecord, make_run_result
from gui_agent.core.run.action_ledger import effective_action_role, semantic_action_key
from gui_agent.core.run.loop import _needs_terminal_reconciliation, _turn_budget_mode
from gui_agent.core.run.result import orchestration_result
from gui_agent.core.run.turns import interactive_turn_count, make_interactive_turn, make_verdict_turn
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


def _step(
    *,
    scope: str = "row:65",
    role: str = "commit",
    kind: str | None = "action",
) -> SupervisorStep:
    return SupervisorStep(
        should_act=True,
        instruction="点击 Save",
        stop=False,
        goal_completed=False,
        summary="",
        milestone_id="m1",
        execution_scope=scope,
        milestone_kind=kind,
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


def test_concrete_scroll_cannot_consume_commit_slot():
    policy = MilestoneSupervisorPolicy()
    mislabeled_scroll = BaseActionDecision(
        action=BaseAction(
            action_type="scroll",
            direction="down",
            description="滚动查看下方选项",
        )
    )
    step = _step(role="commit")
    scroll_turn = make_interactive_turn(
        index=1,
        observation_source="browser",
        supervisor_step=step,
        action_decision=mislabeled_scroll,
        executed=True,
    )

    assert effective_action_role(step, mislabeled_scroll.action) == "iterate"
    assert scroll_turn.action_signal is not None
    assert scroll_turn.action_signal.role == "iterate"
    assert scroll_turn.action_signal.action_key.endswith("|iterate|scroll|down|@-")

    allowed, key, reason = policy.authorize_action_dispatch(
        step, _decision(), [scroll_turn]
    )

    assert allowed is True
    assert key.endswith("|commit")
    assert reason == ""


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


def test_make_turn_records_concrete_write_value():
    step = _step(role="write", kind="filter")
    step.target_control = "Search by keyword"
    decision = BaseActionDecision(action=BaseAction(
        action_type="type",
        x=300,
        y=300,
        text="Minerva LumaTech V-Tee",
        description="type exact search",
    ))

    turn = make_interactive_turn(
        index=1,
        observation_source="browser",
        supervisor_step=step,
        action_decision=decision,
        executed=True,
    )

    assert turn.action_signal is not None
    assert turn.action_signal.target_control == "Search by keyword"
    assert turn.action_signal.target_value == "Minerva LumaTech V-Tee"


def test_duplicate_commit_is_suppressed_before_second_dispatch():
    policy = MilestoneSupervisorPolicy()
    step = _step()
    allowed, _key, reason = policy.authorize_action_dispatch(
        step, _decision(700), [_turn(index=1, step=step)]
    )

    assert allowed is False
    assert "禁止" in reason


def test_filter_commit_is_not_treated_as_at_most_once_business_mutation():
    policy = MilestoneSupervisorPolicy()
    step = _step(scope="milestone:filter", kind="filter")
    prior = _turn(index=1, step=step)

    allowed, _key, reason = policy.authorize_action_dispatch(
        step, _decision(700), [prior]
    )

    assert allowed is True
    assert reason == ""


def test_observation_only_verdict_reconciles_pending_dispatch_without_spending_turn():
    policy = MilestoneSupervisorPolicy()
    dispatched = _turn(index=1, step=_step())
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="milestone",
        action_policy_name="action",
        turns=[dispatched],
    )

    assert _needs_terminal_reconciliation(context) is True
    assert _turn_budget_mode(context, max_turns=1) == "reconcile"
    observation_turn = make_verdict_turn(
        index=2,
        observation_source="browser",
        supervisor_step=SupervisorStep(
            should_act=False,
            stop=False,
            goal_completed=False,
            summary="final state remains incomplete",
            milestone_id="m1",
        ),
        supervisor=policy,
        observation_only=True,
    )
    context.turns.append(observation_turn)

    assert observation_turn.operation_mode == "observation"
    assert interactive_turn_count(context) == 1
    assert _needs_terminal_reconciliation(context) is False
    assert _turn_budget_mode(context, max_turns=1) == "stop"


def test_reconcile_never_invokes_planner_for_incomplete_milestone(monkeypatch):
    milestone = Milestone(
        id="m1",
        name="change target and save",
        description="",
        kind="action",
        success_condition="saved target state is visible",
    )
    policy = MilestoneSupervisorPolicy()
    policy.reseed(milestone)
    monkeypatch.setattr(
        policy,
        "_single_check",
        lambda *_args, **_kwargs: _SingleCheckResult(
            status="in_progress",
            reason="target write was observed but save is still pending",
            summary="save remains",
        ),
    )
    monkeypatch.setattr(
        policy,
        "_invoke_planner",
        lambda *_args, **_kwargs: pytest.fail("reconcile must not invoke planner"),
    )
    monkeypatch.setattr(
        "gui_agent.core.supervisor.milestone.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy.reconcile(
        Observation(png_bytes=b"x", source="browser", dom_state="after-write"),
        "g",
        [],
    )

    assert step.should_act is False
    assert step.goal_completed is False
    assert "save is still pending" in step.summary


def test_action_policy_stop_cannot_complete_an_in_progress_milestone():
    policy = MilestoneSupervisorPolicy()
    step = _step(role="prepare")
    stop = BaseActionDecision(
        action=BaseAction(action_type="stop", description="等待并验证结果")
    )

    allowed, _key, reason = policy.authorize_action_dispatch(step, stop, [])

    assert allowed is False
    assert "无权" in reason


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

    prepare = _step(role="write")
    corrected = _turn(index=2, step=prepare, role="write")
    allowed, _key, _reason = policy.authorize_action_dispatch(
        commit, _decision(), [rejected, corrected]
    )
    assert allowed is True


def test_unverified_feedback_does_not_erase_a_known_commit_contradiction():
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m1",
        name="apply target filter",
        description="",
        success_condition="target filter is applied",
        kind="filter",
    )
    commit = _step(scope="milestone:m1")
    dispatched = _turn(index=1, step=commit)
    signal = dispatched.action_signal
    assert signal is not None

    policy._update_latest_action_outcome(
        [dispatched],
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="the attempted route produced the wrong result",
            summary="route rejected",
            outcome_status="contradicted",
        ),
    )
    policy._update_latest_action_outcome(
        [dispatched],
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="the corrected form is not submitted yet",
            summary="awaiting corrected commit",
            outcome_status="unverified",
        ),
    )

    assert signal.outcome == "contradicted"
    assert signal.outcome_evidence == ["the attempted route produced the wrong result"]

    correction = _turn(index=2, step=_step(scope="milestone:m1", role="write"))
    allowed, _key, reason = policy.authorize_action_dispatch(
        commit,
        _decision(),
        [dispatched, correction],
    )
    assert allowed is True
    assert reason == ""


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
    write_step = _step(scope="milestone:m1", role="write")
    history = [
        _turn(index=1, step=write_step, role="write"),
        _turn(index=2, step=step),
    ]
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


def test_redirected_commit_uses_success_contract_and_is_not_preexisting(monkeypatch):
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m1",
        name="将选项集合持久化包含 XXXL",
        description="",
        success_condition="保存后的选项集合包含 XXXL",
        kind="action",
        require_fresh_action=True,
    )
    policy._milestones = {"m1": milestone}
    policy._current_id = "m1"
    policy._order = ["m1"]
    source_step = _step(scope="row:attribute/144")
    history = [
        _turn(index=1, step=_step(scope="row:attribute/144", role="write"), role="write"),
        _turn(index=2, step=source_step),
    ]
    policy._monitor.observe_effect("http://x/attribute/144", "draft")
    monkeypatch.setattr(
        "gui_agent.core.supervisor.milestone.policy.is_loading_frame",
        lambda _obs: False,
    )
    checker_calls: list[int] = []

    def _unverified_feedback(*_args, **_kwargs):
        checker_calls.append(1)
        return _SingleCheckResult(
            status="in_progress",
            reason="保存请求已响应，但当前帧不能直接读取集合成员",
            summary="提交有响应，结果尚未完全确认",
            outcome_status="unverified",
        )

    monkeypatch.setattr(policy, "_single_check", _unverified_feedback)

    result = policy._run_single_turn(
        milestone,
        Observation(
            png_bytes=b"x",
            source="browser",
            url="http://x/attributes",
            dom_state="list-with-success-toast",
        ),
        history,
    )

    assert result.goal_completed is True
    assert result.completion_status == "accepted_unverified"
    assert result.pre_existing is False
    assert checker_calls == [1]
    assert history[-1].action_signal is not None
    assert history[-1].action_signal.response == "observed"
    assert set(history[-1].action_signal.response_channels) == {"url", "dom"}


def test_redirected_commit_prefers_confirmed_outcome_feedback(monkeypatch):
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m1",
        name="保存记录",
        description="",
        success_condition="记录已保存",
        kind="action",
        require_fresh_action=True,
    )
    policy._milestones = {"m1": milestone}
    policy._current_id = "m1"
    policy._order = ["m1"]
    history = [
        _turn(index=1, step=_step(scope="row:record/65", role="write"), role="write"),
        _turn(index=2, step=_step(scope="row:record/65")),
    ]
    policy._monitor.observe_effect("http://x/record/65", "draft")
    monkeypatch.setattr(
        "gui_agent.core.supervisor.milestone.policy.is_loading_frame",
        lambda _obs: False,
    )
    monkeypatch.setattr(
        policy,
        "_single_check",
        lambda *_args, **_kwargs: _SingleCheckResult(
            status="done",
            reason="当前帧显示保存成功且目标记录状态正确",
            summary="保存结果已确认",
            visible_evidence=["保存成功"],
            outcome_status="confirmed",
        ),
    )

    result = policy._run_single_turn(
        milestone,
        Observation(
            png_bytes=b"x",
            source="browser",
            url="http://x/records",
            dom_state="success",
        ),
        history,
    )

    assert result.goal_completed is True
    assert result.completion_status == "confirmed"
    assert result.pre_existing is False
    assert history[-1].action_signal is not None
    assert history[-1].action_signal.outcome == "confirmed"


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
