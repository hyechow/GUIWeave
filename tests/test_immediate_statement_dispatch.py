from types import SimpleNamespace

from gui_agent.core.orchestrator import Acquire, Command, Data, Finish, OutputSpec, Program
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statements.dispatch import (
    drain_immediate_statements,
    is_immediate_statement,
)
from gui_agent.core.schemas import Observation, PolicyContext, StatementOutcome


def test_acquire_data_and_command_bypass_the_gui_react_loop():
    runtime = ProgramRuntime.start(
        Program(statements=[Data(id="data", goal="derive")])
    )
    assert is_immediate_statement(runtime.current, object()) is True
    acquire = ProgramRuntime.start(Program(statements=[
        Acquire(
            id="collect", goal="collect", bind="rows",
            returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
        )
    ]))
    assert is_immediate_statement(acquire.current, object()) is True


def test_immediate_dispatch_runs_data_then_command_and_records_one_fact_pair_each(
    monkeypatch, tmp_path
):
    from gui_agent.core.run.statements import dispatch

    program = Program(
        statements=[
            Data(id="data", goal="derive destination"),
            Command(
                id="open",
                capability="open_url",
                args={"url": "https://example.test"},
            ),
            Finish(message="done"),
        ]
    )
    context = PolicyContext(
        goal="test",
        supervisor_policy_name="statement",
        action_policy_name="test",
    )
    runtime = ProgramRuntime.start(program, journal=context.journal)
    observation = Observation(png_bytes=b"png", source="browser")
    calls = []
    statuses = []

    def data_executor(invocation, **_kwargs):
        calls.append(invocation.executor)
        return StatementOutcome.completed("derived", observation=observation)

    def command_executor(invocation, **_kwargs):
        calls.append(invocation.executor)
        return StatementOutcome.completed("opened", observation=observation)

    monkeypatch.setattr(dispatch, "execute_data_statement", data_executor)
    monkeypatch.setattr(dispatch, "execute_command", command_executor)
    bundle = SimpleNamespace(prepare_vision_prompt_png=lambda value: value)

    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=bundle,
        platform=object(),
        log_dir=tmp_path,
        check_knowledge="",
        context=context,
        save_context=lambda: None,
        say=lambda _message: None,
        status=statuses.append,
        observation=observation,
        observation_url="screenshot_read_0.png",
    )

    assert result.reply == "done"
    assert calls == ["data", "command"]
    assert statuses[0] == "Data 数据处理中：derive destination"
    assert len(context.journal.turns) == 2
    assert len(context.journal.statement_outcomes) == 2
    assert [turn.statement.executor for turn in context.journal.turns] == [
        "data",
        "command",
    ]
    assert [turn.observation_url for turn in context.journal.turns] == [
        "screenshot_read_0.png",
        "screenshot_read_0.png",
    ]
    assert [event.observation_url for event in context.journal.statement_outcomes] == [
        "screenshot_read_0.png",
        "screenshot_read_0.png",
    ]


def test_data_requirement_propagates_kickback_to_program_recovery(monkeypatch, tmp_path):
    from gui_agent.core.run.statements import dispatch

    context = PolicyContext(
        goal="rank complete history",
        supervisor_policy_name="statement",
        action_policy_name="test",
    )
    runtime = ProgramRuntime.start(
        Program(statements=[Data(id="rank", goal="rank all records")]),
        journal=context.journal,
    )
    observation = Observation(png_bytes=b"png", source="browser")

    monkeypatch.setattr(
        dispatch,
        "execute_data_statement",
        lambda *_args, **_kwargs: StatementOutcome.infeasible(
            "missing email",
            kickback="collect complete rows with customer email",
            observation=observation,
        ),
    )
    result = drain_immediate_statements(
        program_runtime=runtime,
        bundle=SimpleNamespace(prepare_vision_prompt_png=lambda value: value),
        platform=object(),
        log_dir=tmp_path,
        check_knowledge="",
        context=context,
        save_context=lambda: None,
        say=lambda _message: None,
        observation=observation,
    )

    assert result.replan_directive == "collect complete rows with customer email"
    assert result.failure_evidence == "missing email"
    assert context.journal.turns[-1].non_ui["summary"] == "missing email"
