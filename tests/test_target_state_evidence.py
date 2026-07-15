import pytest

from gui_agent.core.run.mutation import authorize_mutation, resolve_mutation
from gui_agent.core.schemas import (
    ActionSignal,
    StatementContract,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
)


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


def test_unique_subject_produces_one_authorized_next_write() -> None:
    statement = _statement()
    subject = _subject(_row("r1", description="XXXL"), statement=statement)
    authorization = authorize_mutation(statement, subject)

    assert (subject.status, subject.next_field) == ("writable", "Admin Swatch")
    assert authorization is not None and authorization.subject_ref == "r1"


def test_singleton_form_can_change_an_existing_value() -> None:
    subject = _subject(
        [{"label": "Amount", "kind": "text_input", "value": "41"}],
        statement=_statement(Amount="42"),
    )
    assert (subject.status, subject.subject_ref) == ("writable", "__form__")


def _choice_group(selected: set[str], *, with_clear: bool = True) -> list[dict]:
    controls = [
        {
            "label": value,
            "option_text": value,
            "kind": "checkbox_input",
            "checked": value in selected,
            "group_id": "choices",
            "group_field": "Size",
        }
        for value in ("S", "M", "L", "38")
    ]
    if with_clear:
        controls[-1]["choice_operations"] = {"clear_all": "Clear choices"}
    return controls


def test_choice_group_uses_clear_command_only_when_it_shortens_reconciliation() -> None:
    bulk = _subject(
        _choice_group({"S", "M", "L"}),
        statement=_statement(Size="38"),
    )
    direct = _subject(
        _choice_group({"S"}),
        statement=_statement(Size="38"),
    )

    assert (bulk.status, bulk.target_control) == ("preparing", "Size Clear choices")
    assert (direct.status, direct.target_control) == ("preparing", "Size S")


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
