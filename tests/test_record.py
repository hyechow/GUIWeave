from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run import turns
from gui_agent.core.schemas import BaseAction, BaseActionDecision, PolicyContext, SupervisorStep


def test_record_interactive_turn_appends_saves_and_emits_callback(monkeypatch):
    ctx = PolicyContext(
        goal="g",
        supervisor_policy_name="milestone",
        action_policy_name="browser",
    )
    supervisor = SimpleNamespace(
        _last_check=None,
        _last_plan=None,
        _last_replan=None,
        _timings={"checker": 1.0},
        _timings_order=["checker"],
        _token_usage={"checker": {"input": 2, "output": 1}},
        _last_sections_loaded=["orders"],
        runtime_state_snapshot=lambda: {},
    )
    step = SupervisorStep(
        should_act=True,
        instruction="点击订单",
        stop=False,
        goal_completed=False,
        summary="准备点击",
    )
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
        read_added_content=True,
        read_note_hash="abc",
        save_context=lambda: saves.append("saved"),
        silent=True,
        on_turn=callbacks.append,
    )

    assert ctx.turns == [turn]
    assert saves == ["saved"]
    assert turn.llm_calls == 2
    assert turn.input_tokens == 20
    assert turn.output_tokens == 10
    assert turn.read_added_content is True
    assert turn.read_note_hash == "abc"
    assert turn.sections_loaded == ["orders"]
    assert callbacks == [{
        "no": 1,
        "summary": "准备点击",
        "executed": True,
        "action_type": "tap",
        "action_desc": "点订单",
    }]
