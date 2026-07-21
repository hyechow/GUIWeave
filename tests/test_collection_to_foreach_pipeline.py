"""End-to-end materialized-records pipeline: Acquire -> Compute -> If -> ForEach.

Proves the design's canonical chain (docs/data_acquisition_and_processing_design.md:113-119):
a complete-coverage collection output materializes as list[record], flows into a Compute node
that filters it, an If guards the empty case, and a ForEach iterates the materialized records
with a fixed body. Deterministic: a stub executor stands in for the GUI/Transition layer while
the real Interpreter owns control flow and the real validation gate enforces complete-coverage
evidence.
"""

from __future__ import annotations

from gui_agent.core.orchestrator import (
    Acquire,
    Compute,
    ComputeRef,
    Condition,
    Read,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    ProgramRunner,
    ValueRef,
)
from gui_agent.core.data_types import FieldRef, FilterStep
from gui_agent.core.schemas import StatementOutcome


ROWS = [
    {"id": "1", "active": "yes"},
    {"id": "2", "active": "no"},
    {"id": "3", "active": "yes"},
    {"id": "4", "active": "no"},
]


def _program() -> Program:
    return Program(
        goal="apply to each active collected record",
        statements=[
            Acquire(
                id="collect",
                bind="observed",
                goal="collect all records in scope",
                returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
            ),
            Compute(
                id="select",
                bind="selection",
                goal="select the active records",
                source="rows",
                inputs={"rows": ValueRef(var="observed", path=["rows"])},
                steps=[FilterStep(
                    field=FieldRef(path=["active"]), cmp="eq", value="yes"
                )],
                outputs={"records": ComputeRef()},
                returns={"records": OutputSpec(type="list[record]")},
            ),
            If(
                cond=Condition(ref=ValueRef(var="selection", path=["records"]), cmp="empty"),
                then=[Finish(message="no active records")],
                otherwise=[
                    ForEach(
                        items=ValueRef(var="selection", path=["records"]),
                        item="record",
                        body=[
                            Interact(
                                id="apply",
                                goal="apply the fixed operation to the current record",
                                success="the current record has been updated",
                                inputs={"record": ValueRef(var="record")},
                            ),
                            Read(
                                id="verify",
                                bind="updated",
                                returns={"ok": OutputSpec(type="boolean")},
                            )
                        ],
                        collect=ValueRef(var="updated", path=["ok"]),
                        into="results",
                    ),
                    Finish(
                        message="applied to {results}",
                        outputs={"results": ValueRef(var="results")},
                    ),
                ],
            ),
        ],
    )


def test_acquire_data_filter_if_foreach_runs_end_to_end():
    calls: list = []

    def execute(invocation):
        calls.append(invocation)
        if invocation.id == "collect":
            # Materialized collection output; complete-coverage requires confirmed evidence.
            return StatementOutcome.completed(
                "collected", verification="confirmed", outputs={"rows": ROWS},
            )
        if invocation.id == "select":
            # Compute consumes the materialized list[record] and filters it.
            rows = invocation.inputs["rows"]
            assert {row["id"] for row in rows} == {"1", "2", "3", "4"}
            active = [row for row in rows if row.get("active") == "yes"]
            return StatementOutcome.completed("selected", outputs={"records": active})
        if invocation.id == "apply":
            return StatementOutcome.completed("applied")
        if invocation.id == "verify":
            return StatementOutcome.completed("verified", outputs={"ok": True})
        raise AssertionError(f"unexpected invocation {invocation.id}")

    result = ProgramRunner(execute).run(_program())

    assert result.failed is False
    # The body ran exactly once per materialized active record (2 of 4).
    apply_calls = [call for call in calls if call.id == "apply"]
    assert [call.loop_path for call in apply_calls] == [[0], [1]]
    assert [call.inputs["record"]["id"] for call in apply_calls] == ["1", "3"]
    assert result.env["results"] == [True, True]
    assert result.reply == "applied to [True, True]"


def test_empty_collection_short_circuits_to_the_then_branch():
    def execute(invocation):
        if invocation.id == "collect":
            return StatementOutcome.completed(
                "collected", verification="confirmed", outputs={"rows": []},
            )
        if invocation.id == "select":
            return StatementOutcome.completed(
                "selected", outputs={"records": []},
            )
        raise AssertionError("ForEach body must not run when no records were collected")

    result = ProgramRunner(execute).run(_program())
    assert result.failed is False
    assert result.reply == "no active records"


def test_complete_coverage_output_without_confirmed_evidence_is_rejected():
    """A complete-coverage collection must carry confirmed evidence (design acceptance)."""

    def execute(invocation):
        if invocation.id == "collect":
            return StatementOutcome.completed(
                "collected", verification="accepted_unverified", outputs={"rows": ROWS},
            )
        raise AssertionError("select must not run when collect failed its coverage contract")

    result = ProgramRunner(execute).run(_program())
    assert result.failed is True
    assert "完整覆盖" in result.reply


def test_program_carries_no_pagination_or_gesture_tokens():
    """Acceptance: Program text contains no scroll counts, page numbers, pagers, or gestures."""
    program = _program()
    blob = program.model_dump_json()
    for forbidden in ("页码", "滚动次数", "scroll_count", "page_index", "next_page", "swipe", "pager"):
        assert forbidden not in blob, f"Program must not encode pagination/gesture token {forbidden!r}"
