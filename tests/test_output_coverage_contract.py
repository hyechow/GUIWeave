"""Output coverage contracts and the runtime complete-coverage gate."""

from __future__ import annotations

from gui_agent.core.orchestrator import (
    Read,
    OutputSpec,
    Program,
    ValueRef,
    validate_program,
)
from gui_agent.core.orchestrator.runner import Interpreter
from gui_agent.core.schemas import StatementOutcome


def _codes(program: Program) -> set[str]:
    return {issue.code for issue in validate_program(program)}


# --- 1. default --------------------------------------------------------------


def test_coverage_field_defaults_to_current_view():
    spec = OutputSpec(type="text")
    assert spec.coverage == "current_view"
    # round-trips through the validator unchanged
    program = Program(
        statements=[
            Read(
                id="read",
                bind="title",
                returns={"title": OutputSpec(type="text")},
            ),
        ]
    )
    assert _codes(program) == set()


# --- 2/3. structural validator rule -----------------------------------------


def test_validator_rejects_complete_coverage_on_non_list_return():
    program = Program(
        statements=[
            Read(
                id="read",
                bind="title",
                returns={"title": OutputSpec(type="text", coverage="complete")},
            ),
        ]
    )
    assert "COVERAGE_REQUIRES_LIST_RECORD" in _codes(program)


def test_validator_accepts_complete_coverage_on_list_record_return():
    program = Program(
        statements=[
            Read(
                id="collect",
                bind="rows",
                returns={"rows": OutputSpec(
                    type="list[record]", coverage="complete", fields=["id"]
                )},
            ),
        ]
    )
    assert "COVERAGE_REQUIRES_LIST_RECORD" not in _codes(program)


# --- 4. runtime complete-coverage gate (structural: reuses verification) ----


def _complete_outcome(verification: str) -> StatementOutcome:
    return StatementOutcome.completed(
        "collected",
        verification=verification,  # type: ignore[arg-type]
        outputs={"rows": [{"id": "1"}, {"id": "2"}]},
    )


def test_validated_outcome_accepts_complete_list_with_confirmed_verification():
    statement = Read(
        id="collect",
        bind="rows",
        returns={"rows": OutputSpec(
            type="list[record]", coverage="complete", fields=["id"]
        )},
    )
    interp = Interpreter(Program(statements=[statement]))
    result = interp._validated_outcome(statement, _complete_outcome("confirmed"))
    assert result.is_completed


def test_validated_outcome_rejects_complete_list_without_confirmed_verification():
    statement = Read(
        id="collect",
        bind="rows",
        returns={"rows": OutputSpec(
            type="list[record]", coverage="complete", fields=["id"]
        )},
    )
    interp = Interpreter(Program(statements=[statement]))
    result = interp._validated_outcome(statement, _complete_outcome("accepted_unverified"))
    assert not result.is_completed
    assert "完整覆盖" in result.summary


def test_validated_outcome_does_not_gate_current_view_or_best_effort_coverage():
    # current_view and best_effort outputs are not subject to the confirmed-evidence gate.
    statement = Read(
        id="collect",
        bind="rows",
        returns={"rows": OutputSpec(
            type="list[record]", coverage="best_effort", fields=["id"]
        )},
    )
    interp = Interpreter(Program(statements=[statement]))
    result = interp._validated_outcome(statement, _complete_outcome("accepted_unverified"))
    assert result.is_completed
