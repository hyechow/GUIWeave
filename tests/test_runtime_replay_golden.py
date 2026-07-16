"""Journal v2 golden replay and process-boundary resume tests."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import PolicyContext
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


FIXTURES = Path(__file__).parent / "fixtures/runtime_replay"


def _fixture(name: str) -> tuple[PolicyContext, dict]:
    root = FIXTURES / name
    context = PolicyContext.model_validate_json(
        (root / "context.json").read_text(encoding="utf-8")
    )
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    return context, expected


def _resume(context: PolicyContext) -> ProgramRuntime:
    revision = context.journal.program_revisions[0]
    program = Program.model_validate(revision.program)
    return ProgramRuntime.resume(program, context.journal)


def test_active_statement_golden_restores_logical_checkpoint() -> None:
    context, expected = _fixture("active_statement")
    runtime = _resume(context)
    policy = StatementSupervisorPolicy()

    assert runtime.current is not None
    policy.resume_statement(
        contract_for_run(runtime.current, runtime.index),
        instance_id=runtime.current_instance_id,
        history=context.journal.turns,
    )

    assert runtime.current.name == expected["current_statement"]
    assert runtime.index == expected["index"]
    assert runtime.current_instance_id == expected["instance_id"]
    assert runtime.notes_mark == expected["notes_mark"]
    assert policy._rt.execution_scope == "i1:s1/statement"
    assert policy._initial_filters == expected["initial_filters"]

    memory = build_memory_view(
        instance_id=runtime.current_instance_id,
        contract=policy._rt.contract,
        history=context.journal.turns,
    )
    assert len(memory.recent_steps) == 1
    assert "type ready" in memory.recent_steps[0].text


def test_terminal_golden_replays_outcome_and_advances_program() -> None:
    context, expected = _fixture("terminal_advance")
    runtime = _resume(context)

    assert runtime.current is not None
    assert runtime.current.name == expected["current_statement"]
    assert runtime.index == expected["index"]
    assert runtime.current_instance_id == expected["instance_id"]
    assert [
        {
            "name": record.name,
            "instance_id": record.instance_id,
            "phase": record.result.phase,
            "reads": record.result.reads,
        }
        for record in runtime.interpreter.run_log
    ] == expected["run_log"]
