from gui_agent.core.schemas import ActionIntent
import pytest

from gui_agent.core.run.execution_signals import CompletionReducer, ExecutionContract
from gui_agent.core.run.mutation import resolve_mutation, resolve_semantic_bindings
from gui_agent.core.schemas import (
    ActionSignal,
    StatementContract,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement.evidence import (
    observed_effect_signal,
    transition_claim,
)
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAssessment,
    _TransitionEvidence,
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


def _semantic_statement(*values: str) -> StatementContract:
    return StatementContract(
        id="members",
        name="ensure semantic members",
        description="",
        success_condition="the collection contains every requested member",
        kind="action",
        target_values={"option_label": list(values) or ["30", "31"]},
    )


def _semantic_control(ref: str, value: str, group: str) -> dict:
    return {
        "kind": "text_input",
        "name": ref,
        "value": value,
        "group_id": group,
    }


def _binding_decision() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="satisfied",
            summary="the requested semantic members are present",
            established_facts=["requested values are visible"],
        ),
        kind="complete",
        reason="the requested semantic members are present",
        evidence=[
            _TransitionEvidence(
                source="current_observation",
                claim="the current collection contains the requested values",
            )
        ],
    )


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


def test_complete_state_index_confirms_values_omitted_from_prompt_inventory() -> None:
    statement = StatementContract(
        id="members",
        name="realize two declared members",
        description="",
        success_condition="the collection contains both declared members",
        kind="action",
        persistence="explicit_commit",
        target_values={
            "Admin Description": ["30", "31"],
            "Admin Swatch": ["30", "31"],
        },
    )
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=_row("collection:13", description="30", swatch="30"),
        form_controls_meta={
            "coverage": "partial",
            "returned": 2,
            "total_rendered": 4,
        },
        form_control_state=[
            *_row("collection:13", description="30", swatch="30"),
            *_row("collection:14", description="31", swatch="31"),
        ],
        form_control_state_meta={
            "coverage": "complete",
            "returned": 4,
            "total_rendered": 4,
            "truncated": False,
        },
    )

    subject = resolve_mutation(statement, observation, [])

    assert subject.status == "complete"
    assert "repeated collection" in subject.evidence


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


def test_semantic_bindings_validate_abstract_contract_against_live_controls() -> None:
    statement = _semantic_statement()
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[
            _semantic_control("option[value][30]", "30", "collection:13"),
            _semantic_control("option[value][31]", "31", "collection:14"),
        ],
    )
    bindings = {
        "option_label=30": ["option[value][30]"],
        "option_label=31": ["option[value][31]"],
    }
    decision = _binding_decision()
    state = resolve_semantic_bindings(statement, observation, bindings)
    assert state.status == "complete"
    evaluation = CompletionReducer().decide(
        ExecutionContract.from_statement(statement),
        [transition_claim(
            decision,
            scope="i1:s1/statement",
            binding_evidence=state.evidence,
        )],
        scope="i1:s1/statement",
    )
    assert evaluation.status == "satisfied"
    assert evaluation.completion_status == "accepted_unverified"


def test_semantic_bindings_require_refs_from_one_observed_subject() -> None:
    statement = _semantic_statement("30")
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[
            _semantic_control("option[value][30]", "30", "collection:13"),
            _semantic_control("description[value][30]", "30", "collection:14"),
        ],
    )
    bindings = {
        "option_label=30": [
            "option[value][30]",
            "description[value][30]",
        ]
    }

    state = resolve_semantic_bindings(statement, observation, bindings)
    assert state.status == "unknown"
    assert "do not belong to one subject" in state.evidence


def test_semantic_bindings_must_cover_every_declared_contract_value() -> None:
    statement = _semantic_statement()
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[
            _semantic_control("option[value][30]", "30", "collection:13")
        ],
    )
    bindings = {"option_label=30": ["option[value][30]"]}

    state = resolve_semantic_bindings(statement, observation, bindings)
    assert state.status == "unknown"
    assert "option_label=31" in state.evidence


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
        supervisor=SupervisorStep(action_intent=ActionIntent(instruction='write target', role='write'), summary='', statement_id='m', statement_kind='action'),
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
