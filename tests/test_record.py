from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

from types import SimpleNamespace

from gui_agent.core.run import turns
from gui_agent.core.schemas import BaseAction, BaseActionDecision, PolicyContext, SupervisorStep


def test_record_interactive_turn_appends_saves_and_emits_callback(monkeypatch):
    ctx = PolicyContext(
        goal="g",
        supervisor_policy_name="statement",
        action_policy_name="browser",
    )
    supervisor = SimpleNamespace(
        _last_transition_record=None,
        _timings={"checker": 1.0},
        _timings_order=["checker"],
        _token_usage={"checker": {"input": 2, "output": 1}},
        _last_sections_loaded=["orders"],
        runtime_state_snapshot=lambda: {},
    )
    step = SupervisorStep(action_intent=ActionIntent(instruction='点击订单'), summary='准备点击')
    action = BaseAction(action_type="tap", x=1, y=2, description="点订单")
    decision = BaseActionDecision(action=action)
    saves = []
    callbacks = []
    monkeypatch.setattr(turns, "get_llm_call_count", lambda: 5)
    monkeypatch.setattr(turns, "get_llm_token_usage", lambda: (100, 50))

    turn = turns.record_interactive_turn(
        context=ctx,
        observation_source="screen.png",
        supervisor_step=step,
        supervisor=supervisor,
        action_decision=decision,
        executed=True,
        llm_calls_before=3,
        tokens_before=(80, 40),
        turn_started_at=0.0,
        save_context=lambda: saves.append("saved"),
        silent=True,
        on_turn=callbacks.append,
    )

    assert ctx.journal.turns == [turn]
    assert saves == ["saved"]
    assert turn.llm_calls == 2
    assert turn.input_tokens == 20
    assert turn.output_tokens == 10
    assert turn.sections_loaded == ["orders"]
    assert callbacks == [{
        "no": 1,
        "summary": "准备点击",
        "executed": True,
        "action_type": "tap",
        "action_desc": "点订单",
    }]
