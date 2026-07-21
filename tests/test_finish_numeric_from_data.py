"""Business values are produced by Read, never by Interact."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gui_agent.core.orchestrator import (
    Read,
    Finish,
    Interact,
    OutputSpec,
    Program,
    ValueRef,
    validate_program,
)


def _codes(program: Program) -> set[str]:
    return {issue.code for issue in validate_program(program)}


@pytest.mark.parametrize("output_type", ["text", "number", "boolean", "record", "list[record]"])
def test_interact_rejects_every_business_output(output_type):
    with pytest.raises(ValidationError, match="adjacent Read"):
        Interact(
            id="read",
            bind="value",
            goal="open the detail page",
            success="the detail page is visible",
            returns={"value": OutputSpec(type=output_type)},
        )


def test_finish_number_from_adjacent_data_is_accepted():
    program = Program(
        goal="count matching reviews",
        statements=[
            Interact(
                id="filter",
                goal="apply the requested review filter",
                success="the filtered review list is visible",
                required_values={"search_term": "best"},
            ),
            Read(
                id="count",
                bind="answer",
                returns={"total": OutputSpec(type="number")},
            ),
            Finish(outputs={"result": ValueRef(var="answer", path=["total"])}),
        ],
    )
    assert _codes(program) == set()


def test_finish_text_from_adjacent_data_is_accepted():
    program = Program(statements=[
        Interact(id="open", goal="open detail", success="detail is visible"),
        Read(
            id="read",
            bind="page",
            returns={"title": OutputSpec(type="text")},
        ),
        Finish(outputs={"result": ValueRef(var="page", path=["title"])}),
    ])
    assert _codes(program) == set()
