import pytest

from gui_agent.core.run.mutation import resolve_mutation
from gui_agent.core.schemas import (
    ActionSignal,
    StatementContract,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement.evidence import observed_effect_signal


def _statement(**values: str) -> StatementContract:
    return StatementContract(
        id="m",
        name="ensure one option",
        description="",
        success_condition="the option exists",
        kind="action",
        target_values=values or {
            "Admin Description": "XXXL",
            "Admin Swatch": "XXXL",
        },
    )


def _row(group: str, description: str = "", swatch: str = "") -> list[dict]:
    return [
        {
            "label": label,
            "kind": "text_input",
            "value": value,
            "group_id": group,
            "group_field": "Admin",
        }
        for label, value in (("Description", description), ("Swatch", swatch))
    ]


def _subject(
    controls: list[dict],
    *,
    coverage: str = "complete",
    history: list[PolicyTurn] | None = None,
    statement: StatementContract | None = None,
):
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=controls,
        form_controls_meta={"coverage": coverage},
    )
    return resolve_mutation(statement or _statement(), observation, history or [])


@pytest.mark.parametrize(
    ("controls", "coverage", "status"),
    [
        ([*_row("r1", description="XXXL"), *_row("r2", swatch="XXXL")], "complete", "ambiguous"),
        ([*_row("r1"), *_row("r2")], "complete", "ambiguous"),
        ([], "partial", "unknown"),
        (_row("r1", description="55 cm", swatch="55 cm"), "complete", "absent"),
    ],
)
def test_subject_resolution_boundaries(
    controls: list[dict], coverage: str, status: str
) -> None:
    assert _subject(controls, coverage=coverage).status == status


def test_unique_subject_projects_one_open_declared_field() -> None:
    statement = _statement()
    subject = _subject(_row("r1", description="XXXL"), statement=statement)

    assert (subject.status, subject.subject_ref) == ("writable", "r1")


def test_aligned_values_resolve_across_repeated_non_choice_rows() -> None:
    statement = StatementContract(
        id="members",
        name="realize two declared members",
        description="",
        success_condition="the collection contains both declared members",
        kind="action",
        target_values={
            "Admin Description": ["30", "31"],
            "Admin Swatch": ["30", "31"],
        },
    )

    controls = [
        *_row("r30", description="30", swatch="30"),
        *_row("r31", description="31", swatch="31"),
    ]
    complete = _subject(controls, statement=statement)
    pending = _subject(
        [
            *_row("r30", description="30", swatch="30"),
            *_row("r31", description="31"),
        ],
        statement=statement,
    )

    assert complete.status == "complete"
    assert complete.subject_ref == ""
    assert pending.status != "complete"
    assert observed_effect_signal(
        statement,
        Observation(png_bytes=b"frame", source="browser", form_controls=controls),
        [],
    ) is not None


def test_abstract_collection_values_project_through_declared_target_controls() -> None:
    statement = StatementContract(
        id="members",
        name="realize two declared members",
        description="",
        success_condition="the collection contains both declared members",
        kind="action",
        target_controls=["Admin Description", "Admin Swatch"],
        target_values={"options_to_add": ["30", "31"]},
    )
    controls = [
        *_row("collection:13", description="30", swatch="30"),
        *_row("collection:14", description="31", swatch="31"),
    ]

    complete = _subject(controls, statement=statement, coverage="partial")
    incomplete = _subject(
        [
            *_row("collection:13", description="30", swatch="30"),
            *_row("collection:14", description="31"),
        ],
        statement=statement,
        coverage="partial",
    )

    assert complete.status == "complete"
    assert incomplete.status != "complete"
    assert observed_effect_signal(
        statement,
        Observation(
            png_bytes=b"frame",
            source="browser",
            form_controls=controls,
            form_controls_meta={"coverage": "partial"},
        ),
        [],
    ) is not None


def test_unrelated_complete_inventory_cannot_disprove_source_local_fields() -> None:
    statement = StatementContract(
        id="members",
        name="realize two declared members",
        description="",
        success_condition="the collection contains both declared members",
        kind="action",
        target_controls=["Admin Description", "Admin Swatch"],
        target_values={"options_to_add": ["30", "31"]},
    )
    destination = Observation(
        png_bytes=b"destination",
        source="browser",
        form_controls=[{
            "label": "Destination Search",
            "kind": "text_input",
            "value": "",
            "group_id": "destination-grid:1",
        }],
        form_controls_meta={"coverage": "complete"},
    )

    assert resolve_mutation(statement, destination, []).status == "unknown"


def test_destination_only_absence_is_not_journaled_as_effect() -> None:
    statement = _statement()
    observation = Observation(
        png_bytes=b"destination",
        source="browser",
        form_controls=[{
            "label": "Search",
            "kind": "text_input",
            "value": "",
            "group_id": "destination-filter",
        }],
        form_controls_meta={"coverage": "complete"},
    )
    assert observed_effect_signal(statement, observation, []) is None


def test_singleton_form_can_change_an_existing_value() -> None:
    subject = _subject(
        [{"label": "Amount", "kind": "text_input", "value": "41"}],
        statement=_statement(Amount="42"),
    )
    assert (subject.status, subject.subject_ref) == ("writable", "__form__")


def _receipt(subject_ref: str = "r1") -> MutationReceipt:
    return MutationReceipt(
        statement_id="m",
        subject_ref=subject_ref,
        field="Admin Description",
        intended_value="XXXL",
        source="structural",
    )


def _write_turn(receipt: MutationReceipt | None, *, no_effect: bool = False) -> PolicyTurn:
    return PolicyTurn(
        index=1,
        observation_source="browser",
        supervisor=SupervisorStep(
            should_act=True,
            instruction="write target",
            summary="",
            statement_id="m",
            statement_kind="action",
            atomic_role="write",
        ),
        executed=True,
        no_effect=no_effect,
        action_signal=ActionSignal(
            role="write",
            execution="dispatched",
            response="none_observed" if no_effect else "unknown",
            mutation_receipt=receipt,
        ),
    )


def test_receipt_binds_observation_and_followup_writes_to_one_subject() -> None:
    history = [_write_turn(_receipt())]
    wrong_row_changed = _subject(
        [*_row("r1"), *_row("r2", description="XXXL")], history=history
    )
    followup = _subject(_row("r1", description="XXXL"), history=history)
    no_effect = _subject(_row("r1"), history=[_write_turn(_receipt(), no_effect=True)])

    assert (wrong_row_changed.subject_ref, wrong_row_changed.status) == ("r1", "writable")
    assert (followup.subject_ref, followup.status) == ("r1", "writable")
    assert (no_effect.subject_ref, no_effect.status) == ("r1", "writable")


def test_unbound_write_does_not_override_current_state_and_conflicting_receipts_are_ambiguous() -> None:
    current = _subject(
        _row("r1", description="XXXL", swatch="55 cm"),
        history=[_write_turn(None)],
    )
    conflicting = _subject(
        [],
        coverage="partial",
        history=[_write_turn(_receipt("r1")), _write_turn(_receipt("r2"))],
    )

    assert current.status == "writable"
    assert conflicting.status == "ambiguous"
