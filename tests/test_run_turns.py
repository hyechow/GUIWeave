from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.run import loop as run_loop
from gui_agent.core.run.turns import (
    make_interactive_turn,
    make_statement_outcome_event,
)
from gui_agent.core.schemas import (
    BaseActionDecision,
    Observation,
    PolicyContext,
    StatementOutcome,
    SupervisorStep,
)


def _step() -> SupervisorStep:
    return SupervisorStep(
        should_act=False,
        outcome=StatementOutcome.completed("完成"),
        summary="完成",
    )


def test_make_statement_outcome_event_captures_supervisor_state():
    supervisor = SimpleNamespace(
        _last_action_plan=None,
        _last_transition_record={
            "proposal": {
                "kind": "complete",
                "reason": "done",
                "effect_status": "confirmed",
            },
            "guard_rejections": [],
        },
        _timings={"checker": 1.2},
        _token_usage={"checker": {"input": 10, "output": 5}},
        _last_sections_loaded=["orders"],
        _context_reports=[{"kind": "context_budget", "label": "checker.dynamic"}],
    )

    event = make_statement_outcome_event(
        after_turn=2,
        observation_source="screen.png",
        supervisor_step=_step().model_copy(update={
            "statement_id": "s1",
            "statement_kind": "action",
        }),
        supervisor=supervisor,
        outcome=StatementOutcome.completed("完成"),
        statement_instance_id="i1:s1",
        llm_calls=1,
        input_tokens=10,
        output_tokens=5,
    )

    assert event.after_turn == 2
    assert event.observation_source == "screen.png"
    assert event.transition is not None
    assert event.transition["proposal"]["kind"] == "complete"
    assert event.timings == {"checker": 1.2}
    assert event.token_usage == {"checker": {"input": 10, "output": 5}}
    assert event.sections_loaded == ["orders"]
    assert event.llm_context == [{"kind": "context_budget", "label": "checker.dynamic"}]


def test_grounding_failure_is_recorded_without_dispatch_evidence():
    step = _step().model_copy(update={"should_act": True, "outcome": None})
    decision = BaseActionDecision(action=None, not_found_reason="当前帧找不到目标")

    turn = make_interactive_turn(
        index=3,
        observation_source="screen.png",
        supervisor_step=step,
        action_decision=decision,
        executed=False,
    )

    assert turn.action_signal is not None
    assert turn.action_signal.execution == "not_attempted"
    assert turn.action_signal.action_key == ""
    assert turn.action_signal.mutation_receipt is None


def test_agent_loop_first_turn_has_no_deferred_loading_state(monkeypatch, tmp_path):
    observation = Observation(png_bytes=b"", source="test")
    def step(*_args):
        return SupervisorStep(
            should_act=False,
            outcome=StatementOutcome.failed("test complete"),
            summary="first observation completed",
        )
    supervisor = SimpleNamespace(
        name="test",
        step=step,
        reconcile=step,
        begin_statement=lambda *a, **k: None,
        end_statement=lambda *_args: None,
    )
    bundle = SimpleNamespace(
        platform="test",
        prepare_vision_prompt_png=lambda data: data,
        make_executor=lambda _platform: SimpleNamespace(prepare_frame=lambda _png: None),
        make_action_visualizer=lambda _platform: None,
        make_perception=lambda *_args: SimpleNamespace(observe=lambda: observation),
    )
    context = PolicyContext(
        goal="test first turn", supervisor_policy_name="test", action_policy_name="test"
    )
    monkeypatch.setattr(run_loop, "build_platform", lambda **_kwargs: bundle)
    monkeypatch.setattr(run_loop, "_load_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(run_loop, "_save_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run_loop, "save_observation_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "gui_agent.core.llm.output.compose_orchestration_reply",
        lambda _goal, _digest, *, current, terminal: terminal,
    )

    result = run_loop.run_agent_loop(
        "test first turn",
        SimpleNamespace(name="test"),
        supervisor,
        None,
        tmp_path,
        tmp_path / "context.json",
        program=Program(goal="test first turn", statements=[Run(name="test first turn")]),
        max_turns=1,
        auto_continue=True,
        silent=True,
        platform=object(),
        headless=True,
    )

    # ProgramRuntime path: a failed statement becomes an interpreter terminal reply.
    assert "test complete" in result.summary
    assert len(context.journal.turns) == 0
    assert len(context.journal.statement_outcomes) == 1
