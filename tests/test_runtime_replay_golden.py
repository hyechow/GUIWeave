"""Journal v2 golden replay and process-boundary resume tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui_agent.core.orchestrator.program import Finish, Program, Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.schemas import Observation, PolicyContext, StatementOutcome, SupervisorStep
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
    assert policy._rt.retry_count == expected["retry_count"]
    assert policy._rt.constraint_ledger.visible(policy._rt.execution_scope) == expected["constraints"]
    assert policy._rt.monitor._progress_values == expected["progress_values"]
    assert policy._rt.monitor._last_url == expected["last_url"]
    assert policy._initial_filters == expected["initial_filters"]


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


def test_agent_loop_resumes_persisted_active_statement(monkeypatch, tmp_path: Path) -> None:
    """A fresh loop process loads context.json and continues the same invocation."""
    from gui_agent.core.run import loop as run_loop

    observation = Observation(png_bytes=b"frame", source="test")
    bundle = SimpleNamespace(
        platform="test",
        prepare_vision_prompt_png=lambda data: data,
        make_executor=lambda _platform: SimpleNamespace(prepare_frame=lambda _png: None),
        make_action_visualizer=lambda _platform: None,
        make_perception=lambda *_args: SimpleNamespace(observe=lambda: observation),
    )
    monkeypatch.setattr(run_loop, "build_platform", lambda **_kwargs: bundle)
    monkeypatch.setattr(run_loop, "save_observation_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    program = Program(
        goal="resume loop",
        statements=[
            Run(statement_id="s1", name="resume loop", kind="action"),
            Finish(message="done"),
        ],
    )
    context_path = tmp_path / "context.json"

    first_policy = StatementSupervisorPolicy()

    def first_step(*_args):
        first_policy._rt.retry_count = 2
        first_policy._rt.constraint_ledger.add(
            "keep the current route",
            scope=first_policy._rt.execution_scope,
            source="loop_guard",
        )
        return SupervisorStep(
            should_act=False,
            summary="still running",
            statement_id="s1",
            execution_scope=first_policy._rt.execution_scope,
        )

    monkeypatch.setattr(first_policy, "step", first_step)
    first = run_loop.run_agent_loop(
        program.goal,
        SimpleNamespace(name="test"),
        first_policy,
        None,
        tmp_path,
        context_path,
        program=program,
        max_turns=1,
        auto_continue=True,
        silent=True,
        platform=object(),
        headless=True,
    )
    persisted = PolicyContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    active_turn = persisted.journal.turns[-1]
    assert first["phase"] == "stopped"
    assert active_turn.runtime_state is not None
    assert active_turn.runtime_state.retry_count == 2

    second_policy = StatementSupervisorPolicy()

    def resumed_step(*_args):
        assert second_policy._rt.retry_count == 2
        assert second_policy._rt.constraint_ledger.visible(
            second_policy._rt.execution_scope
        ) == ["keep the current route"]
        return SupervisorStep(
            should_act=False,
            summary="resumed and complete",
            statement_id="s1",
            execution_scope=second_policy._rt.execution_scope,
            outcome=StatementOutcome.completed("resumed and complete"),
        )

    monkeypatch.setattr(second_policy, "step", resumed_step)
    resumed = run_loop.run_agent_loop(
        program.goal,
        SimpleNamespace(name="test"),
        second_policy,
        context_path,
        tmp_path,
        context_path,
        program=program,
        max_turns=2,
        auto_continue=True,
        silent=True,
        platform=object(),
        headless=True,
    )

    final_context = PolicyContext.model_validate_json(
        context_path.read_text(encoding="utf-8")
    )
    assert resumed["phase"] == "completed"
    assert resumed["verification"] == "confirmed"
    assert final_context.outcome is not None
    assert final_context.outcome.phase == "completed"
    assert len(final_context.journal.program_revisions) == 1
