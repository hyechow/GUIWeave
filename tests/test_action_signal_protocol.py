from __future__ import annotations

import pytest

from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.orchestrator.runner import Interpreter, RunRecord
from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.run.action_signals import effective_action_role, semantic_action_key
from gui_agent.core.run.loop import _needs_terminal_reconciliation, _turn_budget_mode
from gui_agent.core.run.result import orchestration_result
from gui_agent.core.run.turns import interactive_turn_count, make_interactive_turn, make_verdict_turn
from gui_agent.core.schemas import (
    ActionSignal,
    BaseAction,
    BaseActionDecision,
    EffectSignal,
    StatementContract,
    MutationAuthorization,
    MutationReceipt,
    Observation,
    PolicyContext,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.evidence import action_lifecycle_claims, checker_claim
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import _PlanResult, _SingleCheckResult


def test_checker_payload_requires_explicit_effect_status() -> None:
    with pytest.raises(ValueError, match="effect_status"):
        _SingleCheckResult.model_validate({
            "status": "done",
            "reason": "visible state looks complete",
            "summary": "done",
        })


def _step(
    *,
    scope: str = "row:65",
    role: str = "commit",
    kind: str | None = "action",
) -> SupervisorStep:
    return SupervisorStep(
        should_act=True,
        instruction="点击 Save",
        summary="",
        statement_id="m1",
        execution_scope=scope,
        statement_kind=kind,
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
        ),
    )


def test_commit_key_ignores_button_coordinate_within_same_resource():
    step = _step()

    assert semantic_action_key(step, _decision(400).action) == semantic_action_key(
        step, _decision(700).action
    )


def test_write_key_keeps_structural_group_identity():
    action = BaseAction(
        action_type="type",
        x=500,
        y=700,
        text="XXXL",
        description="输入 XXXL",
    )
    def authorized(subject_ref: str) -> SupervisorStep:
        return _step(role="write").model_copy(update={
            "mutation_authorization": MutationAuthorization(
                statement_id="m1",
                subject_ref=subject_ref,
                field="Size",
                desired_value="XXXL",
                source="structural",
            )
        })

    first = authorized("collection:20")
    second = authorized("collection:21")

    assert semantic_action_key(first, action) != semantic_action_key(second, action)


def test_ensure_draft_fields_require_commit_before_statement_advance(monkeypatch):
    """Replay 20260712_122035 T10-T12: matching draft controls are not persistence."""
    statement = StatementContract(
        id="m1",
        name="ensure option member",
        description="",
        success_condition="member fields match and the resource is saved",
        kind="action",
        effect_mode="ensure",
        persistence="explicit_commit",
        target_controls=["options_collection"],
        target_values={
            "Admin Description": "XXXL",
            "Admin Swatch": "XXXL",
        },
    )
    scope = "row:admin/catalog/product_attribute/edit/attribute_id/144"
    resource_url = "http://x/admin/catalog/product_attribute/edit/attribute_id/144"

    def write_turn(index: int, control: str) -> PolicyTurn:
        step = SupervisorStep(
            should_act=True,
            instruction=f"write {control}",
            summary="",
            statement_id="m1",
            statement_kind="action",
            execution_scope=scope,
            atomic_role="write",
            action_family="input",
            target_control=control,
            target_value="XXXL",
            mutation_authorization=MutationAuthorization(
                statement_id="m1",
                subject_ref="collection:19",
                field=control,
                desired_value="XXXL",
                source="structural",
            ),
            requires_mutation_authorization=True,
        )
        decision = BaseActionDecision(action=BaseAction(
            action_type="type",
            x=500,
            y=700,
            text="XXXL",
            description=f"write {control}",
        ))
        return make_interactive_turn(
            index=index,
            observation_source="browser",
            observation_url=resource_url,
            supervisor_step=step,
            action_decision=decision,
            executed=True,
        )

    history = [
        write_turn(10, "Admin Description"),
        write_turn(11, "Admin Swatch"),
    ]
    observation = Observation(
        png_bytes=b"fixture",
        source="browser",
        url=resource_url,
        dom_state="draft-fields-complete",
        form_controls_meta={"coverage": "complete"},
        form_controls=[
            {
                "label": "Description",
                "group_field": "Admin",
                "group_id": "collection:19",
                "kind": "text_input",
                "value": "XXXL",
            },
            {
                "label": "Swatch",
                "group_field": "Admin",
                "group_id": "collection:19",
                "kind": "text_input",
                "value": "XXXL",
            },
        ],
    )
    checker_calls: list[int] = []
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="test:action-signal")
    policy._single_check = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        checker_calls.append(1)
        or _SingleCheckResult(
            status="in_progress",
            reason="draft fields match but Save has not been dispatched",
            summary="commit pending",
            effect_status="unverified",
        )
    )
    policy._invoke_planner = lambda *_args, **_kwargs: _PlanResult(  # type: ignore[method-assign]
        instruction="click Save",
        summary="persist draft",
        atomic_role="commit",
        action_family="activate",
        target_control="Save",
    )
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _observation: False)

    step = policy._run_single_turn(statement, observation, history)

    assert checker_calls == [1]
    assert step.outcome is None
    assert step.should_act is True
    assert step.atomic_role == "commit"
    assert step.action_family == "activate"
    assert step.target_control == "Save"


def test_concrete_scroll_cannot_consume_commit_slot():
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
        action_role=effective_action_role(step, mislabeled_scroll.action),
        action_key=semantic_action_key(step, mislabeled_scroll.action),
    )

    assert effective_action_role(step, mislabeled_scroll.action) == "iterate"
    assert scroll_turn.action_signal is not None
    assert scroll_turn.action_signal.role == "iterate"
    assert scroll_turn.action_signal.action_key.endswith("|iterate|scroll|down|@-")

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
    assert turn.effect_signal is None


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


def test_observation_only_verdict_reconciles_pending_dispatch_without_spending_turn():
    policy = StatementSupervisorPolicy()
    dispatched = _turn(index=1, step=_step())
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="statement",
        action_policy_name="action",
        journal={"events": [dispatched]},
    )

    assert _needs_terminal_reconciliation(context) is True
    assert _turn_budget_mode(context, max_turns=1) == "reconcile"
    observation_turn = make_verdict_turn(
        index=2,
        observation_source="browser",
        supervisor_step=SupervisorStep(
            should_act=False,
            summary="final state remains incomplete",
            statement_id="m1",
        ),
        supervisor=policy,
        observation_only=True,
    )
    context.journal.append_turn(observation_turn)

    assert observation_turn.operation_mode == "observation"
    assert interactive_turn_count(context) == 1
    assert _needs_terminal_reconciliation(context) is False
    assert _turn_budget_mode(context, max_turns=1) == "stop"


def test_reconcile_never_invokes_planner_for_incomplete_statement(monkeypatch):
    statement = StatementContract(
        id="m1",
        name="change target and save",
        description="",
        kind="action",
        success_condition="saved target state is visible",
    )
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="test:action-signal")
    monkeypatch.setattr(
        policy,
        "_single_check",
            lambda *_args, **_kwargs: _SingleCheckResult(
                status="in_progress",
                effect_status="unverified",
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
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy.reconcile(
        Observation(png_bytes=b"x", source="browser", dom_state="after-write"),
        "g",
        [],
    )

    assert step.should_act is False
    assert step.outcome is None
    assert "save is still pending" in step.summary


def test_legacy_effect_does_not_reenter_current_lifecycle_evidence():
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="apply target filter",
        description="",
        success_condition="target filter is applied",
        kind="filter",
    )
    commit = _step(scope="statement:m1")
    dispatched = _turn(index=1, step=commit)
    signal = dispatched.action_signal
    assert signal is not None

    dispatched.effect_signal = EffectSignal(
        statement_id="m1",
        status="contradicted",
        freshness="current_run",
        source_type="legacy.effect",
        authoritative=True,
        evidence=["the attempted route produced the wrong result"],
    )

    assert signal.execution == "dispatched"
    claims = action_lifecycle_claims(
        statement,
        [dispatched],
        scope="statement:m1",
    )
    assert all(item.domain != "effect.state" for item in claims)


def test_unmet_checker_state_is_not_a_failure():
    check = _SingleCheckResult(
        status="in_progress",
        reason="the workflow is still on an intermediate step",
        summary="target remains pending",
        effect_status="unmet",
    )
    assert checker_claim(check, scope="statement:m1").value == "unmet"


@pytest.mark.parametrize("checker_status", ["in_progress", "done"])
def test_terminal_dispatch_without_persistence_response_waits_for_observation(
    monkeypatch, checker_status
):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="保存记录",
        description="",
        success_condition="记录已保存",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
    )
    policy.begin_statement(statement, instance_id="test:action-signal")
    step = _step(scope="statement:m1")
    write_step = _step(scope="statement:m1", role="write")
    history = [
        _turn(index=1, step=write_step, role="write"),
        _turn(index=2, step=step),
    ]
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _obs: False,
    )
    monkeypatch.setattr(
        policy,
        "_single_check",
        lambda *a, **k: _SingleCheckResult(
            status=checker_status,
            reason="未看到成功提示",
            summary="缺少可见反馈",
            effect_status="unverified",
        ),
    )

    result = policy._run_single_turn(
        statement,
        Observation(png_bytes=b"x", source="browser", url="http://x/record/65"),
        history,
    )

    assert result.outcome is None
    assert result.should_act is False


def test_redirected_commit_uses_success_contract_and_is_not_preexisting(monkeypatch):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="将选项集合持久化包含 XXXL",
        description="",
        success_condition="保存后的选项集合包含 XXXL",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
    )
    policy.begin_statement(statement, instance_id="test:action-signal")
    source_step = _step(scope="row:attribute/144")
    history = [
        _turn(index=1, step=_step(scope="row:attribute/144", role="write"), role="write"),
        _turn(index=2, step=source_step),
    ]
    policy._monitor.observe_effect("http://x/attribute/144", "draft")
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _obs: False,
    )
    checker_calls: list[int] = []

    def _unverified_feedback(*_args, **_kwargs):
        checker_calls.append(1)
        return _SingleCheckResult(
            status="in_progress",
            reason="保存请求已响应，但当前帧不能直接读取集合成员",
            summary="提交有响应，结果尚未完全确认",
            effect_status="unverified",
        )

    monkeypatch.setattr(policy, "_single_check", _unverified_feedback)

    result = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"x",
            source="browser",
            url="http://x/attributes",
            dom_state="list-with-success-toast",
        ),
        history,
    )

    assert result.outcome is not None
    assert result.outcome.verification == "accepted_unverified"
    assert result.pre_existing is False
    assert checker_calls == [1]
    assert history[-1].action_signal is not None
    assert history[-1].action_signal.response == "observed"
    assert set(history[-1].action_signal.response_channels) == {"url", "dom"}


def test_redirected_commit_ignores_destination_only_absence(monkeypatch):
    """A destination page cannot disprove source-local fields that disappeared on Save."""
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="persist one record",
        description="",
        success_condition="the saved record contains the requested values",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
        target_controls=["record_fields"],
        target_values={"Primary Value": "A", "Secondary Value": "B"},
    )
    policy.begin_statement(statement, instance_id="test:action-signal")
    write = _turn(
        index=1,
        step=_step(scope="row:record/7", role="write"),
        role="write",
    )
    assert write.action_signal is not None
    write.action_signal.mutation_receipt = MutationReceipt(
        statement_id="m1",
        subject_ref="__form__",
        field="Primary Value",
        intended_value="A",
        source="structural",
    )
    history = [write, _turn(index=2, step=_step(scope="row:record/7"))]
    policy._monitor.observe_effect("http://x/record/7", "draft")
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _obs: False,
    )
    monkeypatch.setattr(
        policy,
        "_single_check",
        lambda *_args, **_kwargs: _SingleCheckResult(
            status="in_progress",
            reason="the destination list does not render the source form fields",
            summary="source-local state is outside this frame",
            missing_evidence=["source form fields"],
            visible_evidence=[],
            effect_status="unmet",
        ),
    )

    result = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"destination",
            source="browser",
            url="http://x/records",
            dom_state="destination-list",
            form_controls_meta={"coverage": "complete"},
            form_controls=[{
                "label": "Destination Search",
                "kind": "text_input",
                "value": "",
            }],
        ),
        history,
    )

    assert result.outcome is not None
    assert result.outcome.verification == "accepted_unverified"
    assert history[-1].action_signal is not None
    assert history[-1].effect_signal is None


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
    result = StatementOutcome.completed(
        "动作已派发，结果未验证",
        verification="accepted_unverified",
    )

    try:
        gen.send(result)
    except StopIteration as exc:
        assert exc.value == "已提交"
    else:  # pragma: no cover - the finish must terminate the tiny program
        raise AssertionError("interpreter did not finish")

    assert result.is_completed
    assert result.verification == "accepted_unverified"
    assert interp.run_log[0].result.verification == "accepted_unverified"


def test_completed_outcome_defaults_to_confirmed():
    result = StatementOutcome.completed("")

    assert result.verification == "confirmed"


def test_final_result_separates_execution_completion_from_outcome_verification(monkeypatch):
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda *_args, **_kwargs: "result remains unverified",
    )
    interp = Interpreter(Program(goal="保存记录", statements=[Finish(message="done")]))
    interp.run_log = [
        RunRecord(
            name="保存记录",
            result=StatementOutcome.completed(
                "动作已派发，结果未验证",
                verification="accepted_unverified",
            ),
        )
    ]
    context = PolicyContext(
        goal="保存记录",
        supervisor_policy_name="statement",
        action_policy_name="action",
    )

    result = orchestration_result(context, interp, "done", current=None)

    assert result["phase"] == "completed"
    assert result["verification"] == "accepted_unverified"
    assert "accepted_unverified" not in result["orchestrator"]
    EffectSignal,
