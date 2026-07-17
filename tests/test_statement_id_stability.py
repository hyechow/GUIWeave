from gui_agent.core.orchestrator import (
    Command,
    ForEach,
    Interact,
    Program,
    ValueRef,
)
from gui_agent.core.orchestrator.program import assign_statement_ids


def test_compiler_assigns_unique_ids_in_depth_first_source_order():
    body = Interact(goal="row action", success="row done")
    program = Program(
        statements=[
            Interact(goal="main action", success="main done"),
            ForEach(items=ValueRef(var="rows"), body=[body]),
            Command(capability="back"),
        ]
    )
    assert assign_statement_ids(program) is program
    assert program.statements[0].id == "s1"
    assert body.id == "s2"
    assert program.statements[2].id == "s3"


def test_assignment_is_idempotent_and_preserves_explicit_ids():
    program = Program(
        statements=[
            Interact(id="named", goal="a", success="a done"),
            Interact(goal="b", success="b done"),
        ]
    )
    assign_statement_ids(program)
    first = [statement.id for statement in program.statements]
    assign_statement_ids(program)
    assert [statement.id for statement in program.statements] == first == ["named", "s2"]
