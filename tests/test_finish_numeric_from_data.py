"""Structural invariant: Finish-consumed numbers must come from Data, not Interact.

Protects the compiler boundary that strips numeric derivation out of the GUI executor.
Trigger sample for FINISH_NUMERIC_FROM_DATA (registry-enforced code).
"""

from __future__ import annotations

from gui_agent.core.orchestrator import (
    Data,
    Finish,
    If,
    Interact,
    OutputSpec,
    Program,
    Condition,
    ValueRef,
    validate_program,
)


def _codes(program: Program) -> set[str]:
    return {issue.code for issue in validate_program(program)}


def test_finish_number_from_interact_is_rejected():
    """Task-15-class shape: Interact returns number → Finish."""
    program = Program(
        goal="count matching reviews",
        statements=[
            Interact(
                id="filter",
                bind="review_count_result",
                goal="filter the review list by the requested term and get the total count",
                success="the filtered grid is visible and the total is known",
                required_values={"search_term": "best"},
                returns={
                    "total_reviews_with_best": OutputSpec(
                        type="number",
                        description="total matching reviews",
                    )
                },
            ),
            Finish(
                outputs={
                    "result": ValueRef(
                        var="review_count_result", path=["total_reviews_with_best"]
                    )
                }
            ),
        ],
    )
    assert "FINISH_NUMERIC_FROM_DATA" in _codes(program)


def test_finish_number_from_data_is_accepted():
    """Canonical shape: Interact prepares UI, Data derives the number, Finish cites Data."""
    program = Program(
        goal="count matching reviews",
        statements=[
            Interact(
                id="filter",
                goal="open the review list and apply the semantic text filter",
                success=(
                    "the review list shows only rows in the requested filter scope"
                ),
                required_values={"search_term": "best"},
            ),
            Data(
                id="count",
                bind="answer",
                goal="derive the total number of reviews in the current filtered scope",
                returns={
                    "total": OutputSpec(
                        type="number",
                        description="count of matching reviews",
                    )
                },
            ),
            Finish(
                outputs={"result": ValueRef(var="answer", path=["total"])},
            ),
        ],
    )
    assert _codes(program) == set()


def test_interact_number_unused_by_finish_is_not_flagged():
    """The gate is about Finish consumption, not every Interact number field."""
    program = Program(
        statements=[
            Interact(
                id="read",
                bind="page",
                goal="open the detail page",
                success="detail is visible",
                returns={"rating": OutputSpec(type="number")},
            ),
            Finish(message="done"),
        ]
    )
    assert "FINISH_NUMERIC_FROM_DATA" not in _codes(program)


def test_finish_text_from_interact_is_allowed():
    program = Program(
        statements=[
            Interact(
                id="read",
                bind="page",
                goal="read the visible title",
                success="title is visible",
                returns={"title": OutputSpec(type="text")},
            ),
            Finish(outputs={"result": ValueRef(var="page", path=["title"])}),
        ]
    )
    assert "FINISH_NUMERIC_FROM_DATA" not in _codes(program)


def test_if_mixed_origin_number_finish_is_rejected():
    """If both branches bind the same var but only one is Data, origin is mixed."""
    program = Program(
        statements=[
            Data(
                id="probe",
                bind="probe",
                goal="see if any rows exist",
                returns={"empty": OutputSpec(type="boolean")},
            ),
            If(
                cond=Condition(
                    ref=ValueRef(var="probe", path=["empty"]),
                    cmp="==",
                    value=True,
                ),
                then=[
                    Data(
                        id="zero",
                        bind="answer",
                        goal="emit zero",
                        returns={"total": OutputSpec(type="number")},
                    )
                ],
                otherwise=[
                    Interact(
                        id="ui_count",
                        bind="answer",
                        goal="read count from the grid chrome",
                        success="count is visible",
                        returns={"total": OutputSpec(type="number")},
                    )
                ],
            ),
            Finish(outputs={"result": ValueRef(var="answer", path=["total"])}),
        ]
    )
    # Shared bind when both branches declare identical returns; origins differ → mixed.
    assert "FINISH_NUMERIC_FROM_DATA" in _codes(program)
