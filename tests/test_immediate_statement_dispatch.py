"""Architecture contracts for immediate statement execution."""

from __future__ import annotations

import inspect

from gui_agent.core.orchestrator.program import Program, Read, Run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statements import drain_immediate_statements, is_immediate_statement
from gui_agent.core.run.statements.navigation import execute_direct_navigation
from gui_agent.core.run.statements.observation import ObservationCursor
from gui_agent.core.run.statements.query import execute_query
from gui_agent.core.run.statements.read import execute_read
from gui_agent.core.schemas import Observation, PolicyContext


def test_single_statement_executors_cannot_advance_interpreter():
    for executor in (execute_read, execute_query, execute_direct_navigation):
        assert "interpreter_steps" not in inspect.signature(executor).parameters


def test_direct_navigation_is_immediate_but_remains_interactive():
    class Client:
        def navigate(self, _url):
            return None

    class Platform:
        client = Client()

    run = Run(kind="navigation", name="打开 https://example.test/item/1")
    assert run.is_interactive
    assert is_immediate_statement(run, Platform())
    assert not is_immediate_statement(run, Platform(), allow_navigation=False)


def test_read_executor_returns_one_outcome_without_dispatch_state(tmp_path):
    run = Read(name="读取当前帧")
    cursor = ObservationCursor(
        bundle=None,
        platform=None,
        log_dir=tmp_path,
        observation=Observation(png_bytes=b"x", source="test"),
        observation_url="frame.png",
    )

    outcome = execute_read(
        run,
        statement_index=0,
        cursor=cursor,
        bundle=None,
        platform=None,
        log_dir=tmp_path,
        check_knowledge="",
        say=lambda _message: None,
        status=lambda _message: None,
    )

    assert outcome.is_completed
    assert outcome.observation_url == "frame.png"
    assert not hasattr(outcome, "current_statement")


def test_dispatcher_uses_program_runtime_until_interactive_run(tmp_path):
    read = Read(name="读取当前帧")
    action = Run(kind="action", name="点击保存")

    runtime = ProgramRuntime.start(Program(statements=[read, action]))
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="test",
        action_policy_name="test",
    )

    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=None,
        platform=object(),
        log_dir=tmp_path,
        check_knowledge="",
        context=context,
        save_context=lambda: None,
        say=lambda _message: None,
        observation=Observation(png_bytes=b"x", source="test"),
        observation_url="frame.png",
    )

    assert runtime.current is action
    assert runtime.index == 1
    assert len(context.journal.turns) == 1
    assert len(runtime.interpreter.run_log) == 1
    instance_id = context.journal.turns[0].statement_instance_id
    assert instance_id
    assert context.journal.turns[0].statement is not None
    assert context.journal.turns[0].statement.id == (read.statement_id or "m0_read")
    assert runtime.interpreter.run_log[0].instance_id == instance_id
