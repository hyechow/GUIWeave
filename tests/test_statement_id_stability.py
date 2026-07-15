"""Compiler-assigned statement ids remain stable through runtime reuse."""

from gui_agent.core.orchestrator.program import ForEach, FunctionDef, Program, Run, assign_statement_ids
from gui_agent.core.orchestrator.recovery import tighten_ui_return_run
from gui_agent.core.run.interactive import contract_for_run


def test_compiler_assigns_unique_ids_across_program_sources():
    body = Run(name="row action", kind="action")
    function_step = Run(name="function action", kind="action")
    program = Program(
        statements=[
            Run(name="main action", kind="action"),
            ForEach(var="row", target="rows", body=[body]),
        ],
        functions=[FunctionDef(name="detail", body=[function_step])],
    )

    assert assign_statement_ids(program) is program
    assert program.statements[0].statement_id == "s1"
    assert body.statement_id == "s2"
    assert function_step.statement_id == "s3"


def test_return_tighten_preserves_source_and_contract_id():
    run = Run(
        statement_id="s1",
        name="打开评论 351",
        kind="navigation",
        returns=["rating"],
    )

    tightened = tighten_ui_return_run(run, missing=["rating"], reads={}, attempt=1)

    assert tightened.statement_id == "s1"
    assert contract_for_run(tightened, 0).id == contract_for_run(run, 0).id == "s1"


def test_assignment_is_idempotent():
    program = Program(statements=[Run(name="a"), Run(name="b")])

    assign_statement_ids(program)
    first = [statement.statement_id for statement in program.statements]
    assign_statement_ids(program)

    assert [statement.statement_id for statement in program.statements] == first == ["s1", "s2"]
