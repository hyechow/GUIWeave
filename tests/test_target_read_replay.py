"""Replay task-185's below-fold target read from 20260728_101134."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.orchestrator.runtime import CodingProgram, CodingProgramRuntime
from gui_agent.core.run.contracts import Command, Interact, Read
from gui_agent.core.schemas import StatementOutcome

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "replay/fixtures/browser/101134_target_read/read_request.json"
)


def test_opening_target_url_does_not_claim_requested_fields_are_available() -> None:
    request = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    source = f"""
def run(ctx):
    ctx.reach("Open products", success={{"entity": "Products"}})
    detail = ctx.read(target={request["target"]!r},
        fields={request["fields"]!r},
    )
    if not detail["Material"]:
        return ctx.query(entity="Products",
            fields={{"Name": "text", "Type": "text", "SKU": "text"}},
            filters={{"SKU": "WH11", "Type": "Configurable Product"}},
        )
    return []
"""
    runtime = CodingProgramRuntime.start(
        CodingProgram(goal="read product material", source=source)
    )

    runtime.send_outcome(StatementOutcome.completed("products available"))
    assert isinstance(runtime.current.statement, Command)
    assert runtime.current_coding_op == "open_target"

    runtime.send_outcome(StatementOutcome.completed("product page opened"))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current.statement.observe_fields == ["Material"]
    assert runtime.current.inputs["ui_state"]["postcondition"]["kind"] == "target_open"
    assert (
        runtime.current.inputs["ui_state"]["postcondition"]["kind"]
        != "target_fields_available"
    )

    runtime.send_outcome(StatementOutcome.completed("Material is readable"))
    assert isinstance(runtime.current.statement, Read)

    runtime.send_outcome(StatementOutcome.completed(
        "Material read",
        outputs={"Material": ""},
    ))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current_coding_op == "restore_source"
    assert runtime.current.statement.expected_state == {
        "entity": "Products",
    }

    runtime.send_outcome(StatementOutcome.completed("products restored"))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current_coding_op == "lookup"
    assert runtime.current.inputs["ui_state"]["token"] == "c1:state"
    runtime.close()
