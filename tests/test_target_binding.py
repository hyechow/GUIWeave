from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.target_binding import (
    BrowserTargetBinder,
)
from gui_agent.core.run.action_exec import ActionExecutor
from gui_agent.core.run.target_binding import bind_action_target
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Observation,
    SupervisorStep,
)


def _step(**updates) -> SupervisorStep:
    step = SupervisorStep(
        should_act=True,
        instruction="set Amount to 42",
        summary="write the declared field",
        statement_id="m1",
        execution_scope="record:1",
        statement_kind="action",
        atomic_role="write",
        action_family="input",
        target_control="Amount",
        target_value="42",
    )
    return step.model_copy(update=updates)


def _decision(x: float | None = 400, y: float | None = 600) -> BaseActionDecision:
    return BaseActionDecision(action=BaseAction(
        action_type="type",
        x=x,
        y=y,
        text="42",
        description="set Amount to 42",
    ))


def test_visual_binding_requires_a_declared_target_and_concrete_point() -> None:
    observation = Observation(png_bytes=b"frame", source="visual")
    assert bind_action_target(
        binder=None,
        step=_step(),
        observation=observation,
        action_decision=_decision(),
    ).status == "bound"
    assert bind_action_target(
        binder=None,
        step=_step(),
        observation=observation,
        action_decision=_decision(None, None),
    ).status == "unresolved"
    assert bind_action_target(
        binder=None,
        step=_step(target_control=""),
        observation=observation,
        action_decision=_decision(),
    ).status == "unresolved"


def test_structural_binding_uses_the_point_owner_in_a_repeated_collection() -> None:
    controls = [
        {
            "label": "Amount",
            "kind": "text_input",
            "group_id": "record:1",
            "rect": {"x": 400, "y": 600},
        },
        {
            "label": "Amount",
            "kind": "text_input",
            "group_id": "record:2",
            "rect": {"x": 400, "y": 700},
        },
    ]
    observation = Observation(
        png_bytes=b"frame", source="browser", form_controls=controls
    )
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=400,
        y=600,
        text="42",
        description="set Amount to 42",
    ))

    bound = BrowserTargetBinder().bind(_step(), observation, decision)
    assert bound is not None
    assert (bound.status, bound.source, bound.unit_id) == (
        "bound",
        "structural",
        "record:1",
    )


def test_native_select_binds_via_target_option_despite_a_bad_label() -> None:
    # Replay of log 20260715_215953 Turn 19: the Type <select> (name=type_id) was labeled
    # 'notice-EV92REG' by the adapter, so name/label matching could not confirm it as the
    # declared 'Type' control and the action was suppressed (3x → run died). The option list
    # is authoritative, so a unique owning select that carries the declared target option
    # binds deterministically — without mutation authorization or a correct label.
    controls = [
        {
            "kind": "native_select",
            "label": "notice-EV92REG",
            "name": "type_id",
            "id": "EV92REG",
            "options": [
                "Simple Product", "Virtual Product", "Bundle Product",
                "Downloadable Product", "Configurable Product", "Grouped Product",
            ],
            "is_filter": True,
            "rect": {"x": 856, "y": 570, "w": 246, "h": 32},
        },
    ]
    observation = Observation(png_bytes=b"frame", source="browser", form_controls=controls)
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="select_option",
        x=854,
        y=569,
        text="Configurable Product",
        description="选择下拉选项 Configurable Product",
    ))
    step = _step(
        statement_kind="filter",
        action_family="select",
        target_control="Type",
        target_value="Configurable Product",
    )

    binding = BrowserTargetBinder().bind(step, observation, decision)

    assert binding is not None
    assert (binding.status, binding.source) == ("bound", "structural")


def test_identity_gap_is_unresolved_not_contradicted() -> None:
    # A declared target whose point-owner cannot be name-confirmed is an identity GAP
    # (unresolved), not a contradiction — there is no positive evidence of a *different*
    # declared control. (Was wrongly 'contradicted', which suppressed correct actions whose
    # label the adapter had mis-extracted.)
    controls = [
        {
            "kind": "text_input",
            "label": "notice-EV92REG",
            "name": "type_id",
            "rect": {"x": 400, "y": 600},
        },
    ]
    observation = Observation(png_bytes=b"frame", source="browser", form_controls=controls)

    binding = BrowserTargetBinder().bind(_step(), observation, _decision())

    assert binding is not None
    assert binding.status == "unresolved"


class _Future:
    def result(self):
        return None


class _Executor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args, **_kwargs) -> bool:
        self.calls += 1
        return True


def _run_action(tmp_path, step: SupervisorStep):
    executor = _Executor()
    result = ActionExecutor().run(
        sv_step=step,
        observation=Observation(png_bytes=b"frame", source="visual"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=1,
        flash=lambda _action: None,
        status=lambda _turn, _message: None,
        say=lambda _message: None,
    )
    return result, executor


def test_write_binding_is_recorded_only_after_a_dispatchable_target(tmp_path) -> None:
    result, executor = _run_action(
        tmp_path, _step(preformed_action=_decision(), atomic_role="prepare")
    )

    assert result.executed is True
    assert executor.calls == 1
    assert result.binding is not None and result.binding.status == "bound"
    turn = make_interactive_turn(
        index=1,
        observation_source="visual",
        supervisor_step=_step(),
        action_decision=result.action_decision,
        executed=True,
        binding=result.binding,
    )
    assert turn.action_signal is not None
    assert turn.action_signal.binding == result.binding


def test_unbound_write_is_suppressed_but_acquire_action_is_not(tmp_path) -> None:
    failed, executor = _run_action(
        tmp_path,
        _step(target_control="", preformed_action=_decision()),
    )
    scroll = BaseActionDecision(action=BaseAction(
        action_type="scroll", direction="down", description="locate the target"
    ))
    acquired, acquire_executor = _run_action(
        tmp_path, _step(preformed_action=scroll)
    )

    assert failed.executed is False
    assert executor.calls == 0
    assert "target binding failed" in failed.suppressed_reason
    assert acquired.executed is True
    assert acquire_executor.calls == 1
    assert acquired.binding is None


def test_bound_write_persists_a_mutation_receipt() -> None:
    step = _step()

    turn = make_interactive_turn(
        index=1,
        observation_source="visual",
        supervisor_step=step,
        action_decision=_decision(),
        executed=True,
        binding=bind_action_target(
            binder=None,
            step=step,
            observation=Observation(png_bytes=b"frame", source="visual"),
            action_decision=_decision(),
        ),
    )

    assert turn.action_signal is not None
    receipt = turn.action_signal.mutation_receipt
    assert receipt is not None
    assert (receipt.subject_ref, receipt.field) == (
        "record:1",
        "Amount",
    )
