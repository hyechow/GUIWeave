import json
from pathlib import Path

from gui_agent.core.run.mutation import resolve_mutation
from gui_agent.core.schemas import (
    StatementContract,
    Observation,
)
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
)
REPLAYS = Path(__file__).resolve().parents[1] / "replay/fixtures/browser"
CHOICE_FIXTURE = REPLAYS / "152920_choice_surface"
INTERMEDIATE_FIXTURE = REPLAYS / "205258_intermediate_transition"


def _observation(root: Path, turn_no: int) -> Observation:
    payload = json.loads(
        (root / f"observation_turn_{turn_no}.json").read_text()
    )
    return Observation.model_validate({
        **payload["observation"],
        "png_bytes": b"structured-replay",
    })


def test_real_152920_choice_surface_resolves_cleanup_then_target_write() -> None:
    from gui_agent.adapters.browser.target_binding import active_choice_controls

    statement = StatementContract(
        id="m8_action",
        name="add one configuration combination",
        description="",
        success_condition="the saved collection contains green and XXXL",
        kind="action",
        target_values={"Color": "green", "Size": "XXXL"},
    )
    states: dict[int, str] = {}
    for turn_no in (24, 25, 26):
        observation = _observation(CHOICE_FIXTURE, turn_no)
        derived = active_choice_controls(observation, statement.target_values)
        normalized = observation.model_copy(update={"form_controls": derived})
        subject = resolve_mutation(statement, normalized, [])
        states[turn_no] = subject.status

        assert len(derived) == 33
        assert [
            item["choice_operations"]
            for item in derived
            if item.get("choice_operations")
        ] == [
            {"select_all": "Select All", "clear_all": "Deselect All"},
            {"select_all": "Select All", "clear_all": "Deselect All"},
        ]

    assert states == {24: "writable", 25: "writable", 26: "writable"}


def test_real_choice_surface_executes_multi_value_contract_as_exact_set() -> None:
    from gui_agent.adapters.browser.target_binding import active_choice_controls

    statement = StatementContract(
        id="multi-choice",
        name="add two configuration combinations",
        description="",
        success_condition="the saved collection contains XXXL-blue and XXXL-purple",
        kind="action",
        target_values={"Size": "XXXL", "Color": ["Blue", "Purple"]},
    )
    source = _observation(CHOICE_FIXTURE, 26)
    derived = list(active_choice_controls(source, statement.target_values))
    assert len(derived) == 33

    def state(*selected: str):
        selected_keys = {value.casefold() for value in selected}
        controls = [
            {
                **control,
                "checked": str(control.get("option_text", "")).casefold() in selected_keys,
                "value": (
                    "on"
                    if str(control.get("option_text", "")).casefold() in selected_keys
                    else "off"
                ),
            }
            for control in derived
        ]
        return resolve_mutation(
            statement,
            Observation(png_bytes=b"replay", source="browser", form_controls=controls),
            [],
        )

    assert state().status == "writable"
    assert state("XXXL").status == "writable"
    assert state("XXXL", "Blue").status == "writable"
    assert state("XXXL", "Blue", "Purple").status == "complete"
    assert state("XXXL", "Blue", "Purple", "Green").status == "writable"
    assert resolve_mutation(
        statement, Observation(png_bytes=b"visual-only", source="browser"), []
    ).status == "unknown"


def test_real_205258_completed_choice_set_keeps_intermediate_transition_prepare() -> None:
    from gui_agent.adapters.browser.target_binding import (
        active_choice_controls,
        active_surface_id,
    )
    from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy

    statement = StatementContract(
        id="m9_action",
        name="persist the declared configuration combinations",
        description="",
        success_condition="the saved collection contains both declared combinations",
        kind="action",
        target_values={"Size": "XXS", "Color": ["blue", "purple"]},
        persistence="explicit_commit",
    )
    observation = _observation(INTERMEDIATE_FIXTURE, 28)
    policy = StatementSupervisorPolicy(
        mutation_control_resolver=active_choice_controls,
        surface_resolver=active_surface_id,
    )
    policy.begin_statement(statement, instance_id="i1")
    normalized = policy._mutation_observation(  # noqa: SLF001 - replay production seam
        observation,
        statement.target_values,
    )
    subject = resolve_mutation(
        statement,
        normalized,
        [],
    )
    decision = _StatementTransitionResult(
        kind="act",
        reason="declared choices are complete but the workflow has not reached persistence",
        summary="the local choices are complete; continue to the next workflow surface",
        action=_TransitionAction(
            instruction="advance the current workflow",
            atomic_role="prepare",
            action_family="activate",
            target_control="Next",
        ),
    )
    step, rejection = policy._materialize_transition_action(  # noqa: SLF001
        decision,
        statement,
        execution_scope=policy._scope_for(statement, normalized),
    )

    assert rejection == "" and step is not None
    assert subject.status == "complete"
    assert step.should_act is True
    assert step.atomic_role == "prepare"
    assert step.target_control == "Next"
