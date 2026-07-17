from __future__ import annotations

import json

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.core.schemas import ActionIntent

from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.orchestrator.runner import Interpreter, RunRecord
from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.run.action_signals import effective_action_role, semantic_action_key
from gui_agent.core.run.result import orchestration_result
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.schemas import (
    ActionSignal,
    BaseAction,
    BaseActionDecision,
    EffectSignal,
    StatementContract,
    MutationReceipt,
    Observation,
    PolicyContext,
    PolicyTurn,
    SupervisorStep,
    TargetBinding,
)
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.evidence import action_lifecycle_claims
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


def _assessment(status: str, summary: str) -> _TransitionAssessment:
    return _TransitionAssessment(
        status=status,
        summary=summary,
        open_gaps=[summary] if status == "in_progress" else [],
    )


def _step(
    *,
    scope: str = "row:65",
    role: str = "commit",
    kind: str | None = "action",
) -> SupervisorStep:
    return SupervisorStep(action_intent=ActionIntent(instruction='点击 Save', role=role), summary='', statement_id='m1', execution_scope=scope, statement_kind=kind)


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
    signal_role = role or step.action_intent.role
    key = semantic_action_key(step, decision.action)
    return PolicyTurn(
        index=index,
        observation_source="browser",
        statement_instance_id="test:action-signal",
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


def test_exact_transport_key_distinguishes_document_refs():
    step = _step(role="iterate")
    first = BrowserAction(
        action_type="scroll_to_ref",
        target_ref=41,
        description="bring first target into view",
    )
    second = BrowserAction(
        action_type="scroll_to_ref",
        target_ref=42,
        description="bring second target into view",
    )

    assert semantic_action_key(step, first) != semantic_action_key(step, second)


def test_ensure_draft_fields_require_commit_before_statement_advance(monkeypatch):
    """Replay 20260712_122035 T10-T12: matching draft controls are not persistence."""
    statement = StatementContract(
        id="m1",
        name="ensure option member",
        description="",
        success_condition="member fields match and the resource is saved",
        kind="action",
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
        step = SupervisorStep(action_intent=ActionIntent(instruction=f'write {control}', role='write', family='input', target_control=control, target_value='XXXL'), summary='', statement_id='m1', statement_kind='action', execution_scope=scope)
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
            binding=TargetBinding(
                status="bound",
                source="structural",
                unit_id="collection:19",
            ),
            statement_instance_id="test:action-signal",
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
    transition_calls: list[int] = []
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="test:action-signal")
    policy._invoke_statement_transition = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        transition_calls.append(1)
        or _StatementTransitionResult(
            assessment=_assessment("in_progress", "persist draft"),
            kind="act",
            reason="draft fields match but Save has not been dispatched",
            summary="persist draft",
            action=_TransitionAction(
                instruction="click Save",
                atomic_role="commit",
                action_family="activate",
                target_control="Save",
                expected_result="Save produces a persistence response",
            ),
        )
    )
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _observation: False)

    step = policy._run_single_turn(statement, observation, history)

    assert transition_calls == [1]
    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.role == "commit"
    assert step.action_intent.family == "activate"
    assert step.action_intent.target_control == "Save"


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
    step = _step(role="write", kind="filter").model_copy(update={
        "action_intent": ActionIntent(
            instruction="type exact search",
            role="write",
            family="input",
            target_control="Search by keyword",
        )
    })
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


def test_reconcile_vetoes_transition_action_for_incomplete_statement(monkeypatch):
    statement = StatementContract(
        id="m1",
        name="change target and save",
        description="",
        kind="action",
        success_condition="saved target state is visible",
    )
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="test:action-signal")
    decisions: list[int] = []
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: (
        decisions.append(1)
        or _StatementTransitionResult(
            assessment=_assessment("in_progress", "save remains"),
            kind="act",
            reason="save remains",
            summary="save remains",
            action=_TransitionAction(
                instruction="click Save",
                atomic_role="commit",
                action_family="activate",
                target_control="Save",
                expected_result="Save produces a persistence response",
            ),
        )
    ))
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy.reconcile(
        Observation(png_bytes=b"x", source="browser", dom_state="after-write"),
        "g",
        [],
    )

    assert step.action_intent is None
    assert step.outcome is not None
    assert step.outcome.phase == "exhausted"
    assert len(decisions) == 1
    assert "hard-budget final frame" in step.summary


def test_authoritative_negative_effect_reenters_lifecycle_evidence():
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
        source_type="adapter.validation",
        authoritative=True,
        evidence=["the attempted route produced the wrong result"],
    )

    assert signal.execution == "dispatched"
    claims = action_lifecycle_claims(
        statement,
        [dispatched],
        scope="statement:m1",
    )
    effect = next(item for item in claims if item.domain == "effect.state")
    assert effect.value == "contradicted"
    assert effect.authoritative


def test_terminal_dispatch_without_persistence_response_can_choose_an_action(monkeypatch):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="保存记录",
        description="",
        success_condition="记录已保存",
        kind="action",
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
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k:
        _StatementTransitionResult(
            assessment=_assessment("in_progress", "缺少可见反馈"),
            kind="act",
            reason="未看到成功提示",
            summary="缺少可见反馈",
            action=_TransitionAction(
                instruction="点击刷新以验证保存结果",
                atomic_role="prepare",
                action_family="activate",
                target_control="Refresh",
                expected_result="the saved state becomes observable",
            ),
        )
    )

    result = policy._run_single_turn(
        statement,
        Observation(png_bytes=b"x", source="browser", url="http://x/record/65"),
        history,
    )

    assert result.outcome is None
    assert result.action_intent is not None


def test_redirected_commit_uses_success_contract_and_is_not_preexisting(monkeypatch):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="将选项集合持久化包含 XXXL",
        description="",
        success_condition="保存后的选项集合包含 XXXL",
        kind="action",
        persistence="explicit_commit",
    )
    policy.begin_statement(statement, instance_id="test:action-signal")
    source_step = _step(scope="row:attribute/144")
    history = [
        _turn(index=1, step=_step(scope="row:attribute/144", role="write"), role="write"),
        _turn(index=2, step=source_step),
    ]
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _obs: False,
    )
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _StatementTransitionResult(
            assessment=_assessment("satisfied", "save receipt and destination are visible"),
            kind="complete",
            reason="Save dispatch is in memory and the destination page is visible",
            evidence=[_TransitionEvidence(
                source="journal", event_ref="turn:2", claim="Save was dispatched"
            )],
        ),
    )

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
    assert history[-1].action_signal is not None


def test_redirected_commit_ignores_destination_only_absence(monkeypatch):
    """A destination page cannot disprove source-local fields that disappeared on Save."""
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m1",
        name="persist one record",
        description="",
        success_condition="the saved record contains the requested values",
        kind="action",
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
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _obs: False,
    )
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _StatementTransitionResult(
            assessment=_assessment("satisfied", "write and Save are Journal facts"),
            kind="complete",
            reason="the source write and Save dispatch are Journal facts",
            evidence=[_TransitionEvidence(
                source="journal", event_ref="turn:2", claim="Save was dispatched"
            )],
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

    assert result.phase == "completed"
    assert result.verification == "accepted_unverified"
    assert "accepted_unverified" not in result.orchestrator


def test_report_run_log_never_serializes_live_observation_bytes(monkeypatch):
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda *_args, **_kwargs: "done",
    )
    interp = Interpreter(Program(goal="open target", statements=[Finish(message="done")]))
    interp.run_log = [
        RunRecord(
            name="open target",
            result=StatementOutcome.completed(
                "opened",
                observation=Observation(png_bytes=b"\x89PNG\r\n", source="browser"),
                observation_url="screenshot_nav_1.png",
            ),
        )
    ]
    context = PolicyContext(
        goal="open target",
        supervisor_policy_name="statement",
        action_policy_name="action",
    )

    result = orchestration_result(context, interp, "done")
    payload = result.model_dump(mode="json")

    json.dumps(payload)
    logged = result.orchestrator["run_log"][0]["result"]
    assert "observation" not in logged
    assert logged["observation_url"] == "screenshot_nav_1.png"
    EffectSignal,
