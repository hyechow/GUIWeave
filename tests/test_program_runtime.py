"""ProgramRuntime ownership: always-on program, no DAG fallback."""

from __future__ import annotations

import pytest

from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.run.program_runtime import (
    ProgramRuntime,
    compile_single_statement_program,
    ensure_program,
)
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.schemas import Observation


def test_ensure_program_never_returns_none():
    assert ensure_program(None, "打开设置").statements
    p = Program(goal="g", statements=[Run(name="a", kind="action")])
    assert ensure_program(p, "ignored") is p


def test_compile_single_statement_is_one_action_run():
    program = compile_single_statement_program("在设置里打开蓝牙")
    assert len(program.statements) == 1
    assert isinstance(program.statements[0], Run)
    assert program.statements[0].kind == "action"


def test_program_runtime_starts_and_finishes_finish_only_program():
    program = Program(goal="done", statements=[Finish(message="ok")])
    rt = ProgramRuntime.start(program)
    assert rt.finished
    assert rt.reply == "ok"
    assert rt.current is None


def test_program_runtime_send_advances_cursor():
    program = Program(
        goal="g",
        statements=[
            Run(name="one", kind="action", var="a"),
            Run(name="two", kind="action", var="b"),
        ],
    )
    rt = ProgramRuntime.start(program)
    assert rt.current is not None and rt.current.name == "one"
    from gui_agent.core.orchestrator.program import RunResult

    nxt = rt.send(RunResult(completed=True, summary="done one"))
    assert nxt is not None and nxt.name == "two"
    assert rt.index == 1


def test_supervisor_step_without_reseed_raises():
    policy = MilestoneSupervisorPolicy()
    obs = Observation(png_bytes=b"x", source="test")
    with pytest.raises(RuntimeError, match="reseed"):
        policy.step(obs, "goal", [])


def test_supervisor_reseed_then_complete_does_not_walk_next_milestone():
    """Single-statement executor: completing one statement does not open the next."""
    from gui_agent.core.run.execution_signals import CompletionEvaluation
    from gui_agent.core.schemas import Milestone

    policy = MilestoneSupervisorPolicy()
    first = Milestone(
        id="m1",
        name="第一步",
        description="d",
        success_condition="done",
        kind="action",
    )
    second = Milestone(
        id="m2",
        name="第二步",
        description="d",
        success_condition="done",
        kind="action",
    )
    # Simulate accidental multi-order (old DAG shape) — complete must still terminal.
    policy._milestones = {first.id: first, second.id: second}
    policy._order = [first.id, second.id]
    policy._current_id = first.id
    first.status = "running"

    decision = CompletionEvaluation(
        status="satisfied",
        reason="ok",
        completion_status="confirmed",
        next="complete",
    )
    step = policy._advance(
        first,
        Observation(png_bytes=b"x", source="t"),
        [],
        decision=decision,
    )
    assert step.goal_completed is True
    assert step.stop is True
    assert policy._current_id is None
