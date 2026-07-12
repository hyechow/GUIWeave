from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.helpers import target_value_state


def _milestone() -> Milestone:
    return Milestone(
        id="m",
        name="ensure one option",
        description="",
        success_condition="the option exists",
        kind="action",
        target_values={"Admin Description": "XXXL", "Admin Swatch": "XXXL"},
    )


def _row(group: str, description: str = "", swatch: str = "") -> list[dict]:
    return [
        {
            "label": "Description",
            "kind": "text_input",
            "value": description,
            "group_id": group,
            "group_field": "Admin",
        },
        {
            "label": "Swatch",
            "kind": "text_input",
            "value": swatch,
            "group_id": group,
            "group_field": "Admin",
        },
    ]


def test_target_state_requires_values_in_the_same_structural_unit() -> None:
    controls = [*_row("row:1", description="XXXL"), *_row("row:2", swatch="XXXL")]

    state = target_value_state(controls, _milestone(), coverage="complete")

    assert state.status != "complete"


def test_target_state_reports_one_partial_unit_as_evidence() -> None:
    state = target_value_state(
        _row("row:1", description="XXXL"),
        _milestone(),
        coverage="complete",
    )

    assert state.status == "partial"
    assert state.group_id == "row:1"
    assert state.missing_fields == ("Admin Swatch",)


def test_target_state_reports_ambiguous_blank_units_without_selecting_one() -> None:
    state = target_value_state(
        [*_row("row:1"), *_row("row:2")],
        _milestone(),
        coverage="complete",
    )

    assert state.status == "ambiguous"


def test_target_state_partial_coverage_does_not_claim_absence() -> None:
    state = target_value_state([], _milestone(), coverage="partial")

    assert state.status == "unknown"
