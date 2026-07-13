import pytest

from gui_agent.core.run.mutation import authorize_mutation, resolve_mutation
from gui_agent.core.schemas import (
    ActionSignal,
    Milestone,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
)


def _milestone(**values: str) -> Milestone:
    return Milestone(
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
    milestone: Milestone | None = None,
):
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=controls,
        form_controls_meta={"coverage": coverage},
    )
    return resolve_mutation(milestone or _milestone(), observation, history or [])


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


def test_unique_subject_produces_one_authorized_next_write() -> None:
    milestone = _milestone()
    subject = _subject(_row("r1", description="XXXL"), milestone=milestone)
    authorization = authorize_mutation(milestone, subject)

    assert (subject.status, subject.next_field) == ("writable", "Admin Swatch")
    assert authorization is not None and authorization.subject_ref == "r1"


def test_singleton_form_can_change_an_existing_value() -> None:
    subject = _subject(
        [{"label": "Amount", "kind": "text_input", "value": "41"}],
        milestone=_milestone(Amount="42"),
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
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m",
            milestone_kind="action",
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
    assert (followup.subject_ref, followup.next_field) == ("r1", "Admin Swatch")
    assert (no_effect.subject_ref, no_effect.status) == ("r1", "writable")


def test_unbound_or_conflicting_receipts_cannot_authorize_a_subject() -> None:
    contaminated = _subject(
        _row("r1", description="XXXL", swatch="55 cm"),
        history=[_write_turn(None)],
    )
    conflicting = _subject(
        [],
        coverage="partial",
        history=[_write_turn(_receipt("r1")), _write_turn(_receipt("r2"))],
    )

    assert contaminated.status == "ambiguous"
    assert conflicting.status == "ambiguous"
