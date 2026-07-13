from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.target_binding import BrowserTargetBinder
from gui_agent.core.run.action_exec import ActionExecutionState
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
        stop=False,
        goal_completed=False,
        summary="write the declared field",
        milestone_id="m1",
        execution_scope="record:1",
        milestone_kind="action",
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
    wrong_unit = BrowserTargetBinder().bind(
        _step(target_group_id="record:2"), observation, decision
    )

    assert bound is not None
    assert (bound.status, bound.source, bound.unit_id) == (
        "bound",
        "structural",
        "record:1",
    )
    assert wrong_unit is not None
    assert wrong_unit.status == "contradicted"


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
    result = ActionExecutionState().run(
        sv_step=step,
        observation=Observation(png_bytes=b"frame", source="visual"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=object(),
        platform=object(),
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
