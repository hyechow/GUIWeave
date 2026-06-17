from __future__ import annotations

from gui_agent.core.run import flow
from gui_agent.core.schemas import PolicyContext


def _context() -> PolicyContext:
    return PolicyContext(
        goal="g",
        supervisor_policy_name="milestone",
        action_policy_name="browser",
    )


def test_handle_loading_frame_waits_and_continues(monkeypatch):
    sleeps = []
    messages = []
    monkeypatch.setattr(flow.time, "sleep", sleeps.append)

    result = flow.handle_loading_frame(
        loading_streak=0,
        max_loading_frames=12,
        wait_s=0.6,
        turn_no=2,
        program=None,
        current_run=None,
        context=_context(),
        interpreter=None,
        finish=lambda value: value,
        stop_after_esc=lambda turn_no: None,
        say=messages.append,
    )

    assert result.streak == 1
    assert result.continue_loop is True
    assert result.terminal_result is None
    assert sleeps == [0.6]
    assert messages == ["  [Loading] 等待页面稳定（第 1 帧，不计入轮数）..."]


def test_handle_loading_frame_returns_esc_interrupt(monkeypatch):
    monkeypatch.setattr(flow.time, "sleep", lambda _s: None)

    result = flow.handle_loading_frame(
        loading_streak=1,
        max_loading_frames=12,
        wait_s=0.6,
        turn_no=3,
        program=None,
        current_run=None,
        context=_context(),
        interpreter=None,
        finish=lambda value: value,
        stop_after_esc=lambda turn_no: {"stop_reason": f"esc {turn_no}"},
        say=lambda _message: None,
    )

    assert result.streak == 2
    assert result.continue_loop is False
    assert result.terminal_result == {"stop_reason": "esc 3"}


def test_handle_loading_frame_stops_after_limit():
    messages = []

    result = flow.handle_loading_frame(
        loading_streak=12,
        max_loading_frames=12,
        wait_s=0.6,
        turn_no=4,
        program=None,
        current_run=None,
        context=_context(),
        interpreter=None,
        finish=lambda value: {"wrapped": value},
        stop_after_esc=lambda turn_no: None,
        say=messages.append,
    )

    assert result.streak == 13
    assert result.continue_loop is False
    assert result.terminal_result["wrapped"]["stop_reason"] == "页面持续加载未稳定（>12 帧）"
    assert messages == ["\n页面持续加载 13 帧仍未稳定，agent-loop 停止"]
