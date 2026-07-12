"""Architecture contracts for immediate statement execution."""

from __future__ import annotations

import inspect

from gui_agent.core.orchestrator.program import Read, Run
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

    assert outcome.result.completed
    assert outcome.observation_url == "frame.png"
    assert not hasattr(outcome, "current_statement")


def test_dispatcher_alone_resumes_generator_until_milestone_run(tmp_path):
    read = Read(name="读取当前帧")
    action = Run(kind="action", name="点击保存")

    def steps():
        read_result = yield read
        assert read_result.completed
        yield action

    generator = steps()
    first = next(generator)
    context = PolicyContext(
        goal="g",
        supervisor_policy_name="test",
        action_policy_name="test",
    )

    result = drain_immediate_statements(
        current_statement=first,
        statement_index=0,
        interpreter_steps=generator,
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

    assert result.current_statement is action
    assert result.statement_index == 1
    assert len(context.turns) == 1
