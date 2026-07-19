"""Phase 4: OutputSpec.coverage field, structural validator rule, runtime complete-coverage
gate, and Data SqlOp require_complete derivation. Deterministic, no LLM.
"""

from __future__ import annotations

from gui_agent.core.orchestrator import (
    Data,
    Interact,
    OutputSpec,
    Program,
    ValueRef,
    validate_program,
)
from gui_agent.core.orchestrator.runner import Interpreter, StatementInvocation
from gui_agent.core.run.statements.data import SqlOp, _derive_require_complete
from gui_agent.core.schemas import StatementOutcome


def _codes(program: Program) -> set[str]:
    return {issue.code for issue in validate_program(program)}


# --- 1. backward-compatible default -----------------------------------------


def test_coverage_field_defaults_to_current_view_for_existing_programs():
    spec = OutputSpec(type="text")
    assert spec.coverage == "current_view"
    # round-trips through the validator unchanged
    program = Program(
        statements=[
            Interact(
                id="read",
                bind="title",
                goal="read title",
                success="title read",
                returns={"title": OutputSpec(type="text")},
            ),
        ]
    )
    assert _codes(program) == set()


# --- 2/3. structural validator rule -----------------------------------------


def test_validator_rejects_complete_coverage_on_non_list_return():
    program = Program(
        statements=[
            Interact(
                id="read",
                bind="title",
                goal="read title",
                success="title read",
                returns={"title": OutputSpec(type="text", coverage="complete")},
            ),
        ]
    )
    assert "COVERAGE_REQUIRES_LIST_RECORD" in _codes(program)


def test_validator_accepts_complete_coverage_on_list_record_return():
    program = Program(
        statements=[
            Interact(
                id="collect",
                bind="rows",
                goal="collect rows",
                success="all rows observed",
                returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
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
    statement = Interact(
        id="collect",
        bind="rows",
        goal="collect rows",
        success="all rows observed",
        returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
    )
    interp = Interpreter(Program(statements=[statement]))
    result = interp._validated_outcome(statement, _complete_outcome("confirmed"))
    assert result.is_completed


def test_validated_outcome_rejects_complete_list_without_confirmed_verification():
    statement = Interact(
        id="collect",
        bind="rows",
        goal="collect rows",
        success="all rows observed",
        returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
    )
    interp = Interpreter(Program(statements=[statement]))
    result = interp._validated_outcome(statement, _complete_outcome("accepted_unverified"))
    assert not result.is_completed
    assert "完整覆盖" in result.summary


def test_validated_outcome_does_not_gate_current_view_or_best_effort_coverage():
    # current_view and best_effort outputs are not subject to the confirmed-evidence gate.
    statement = Interact(
        id="collect",
        bind="rows",
        goal="collect rows",
        success="rows read",
        returns={"rows": OutputSpec(type="list[record]", coverage="best_effort")},
    )
    interp = Interpreter(Program(statements=[statement]))
    result = interp._validated_outcome(statement, _complete_outcome("accepted_unverified"))
    assert result.is_completed


# --- 5. Data SqlOp require_complete derives from coverage -------------------


def _invocation(returns):
    return StatementInvocation(
        statement=Data(
            id="derive",
            goal="derive outputs",
            returns=returns,
        )
    )


def _sql_op() -> SqlOp:
    return SqlOp(
        kind="sql",
        name="q",
        source="rows",
        sql="SELECT COUNT(*) AS count FROM rows",
        returns=["count"],
    )


def test_derive_require_complete_honors_best_effort_coverage():
    best_effort = _invocation({"count": OutputSpec(type="number", coverage="best_effort")})
    assert _derive_require_complete(best_effort, _sql_op()) is False


def test_derive_require_complete_keeps_true_for_complete_and_default_coverage():
    complete = _invocation({"count": OutputSpec(type="number", coverage="complete")})
    assert _derive_require_complete(complete, _sql_op()) is True
    default = _invocation({"count": OutputSpec(type="number")})
    assert _derive_require_complete(default, _sql_op()) is True
