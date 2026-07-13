from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run import action_exec
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    SupervisorStep,
    TargetVerify,
)


class _CdpPlatform:
    def __init__(self, frame: bytes) -> None:
        self.frame = frame

    def wait_settled(self, _action_type):
        return 0.4, True

    def screenshot(self):
        return self.frame


class _Pool:
    def __init__(self) -> None:
        self.submitted = None

    def submit(self, fn, *args):
        self.submitted = (fn, args)
        return "future"


class _Future:
    def __init__(self, value) -> None:
        self.value = value
        self.timeout = None

    def result(self, *, timeout=None):
        self.timeout = timeout
        return self.value


def _step() -> SupervisorStep:
    return SupervisorStep(
        should_act=True,
        instruction="点击确认按钮",
        stop=False,
        goal_completed=False,
        summary="需要点击",
    )


def test_submit_target_verify_uses_tap_point():
    pool = _Pool()
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)

    future = action_exec.submit_target_verify(
        action_decision=decision,
        executed=True,
        sv_step=_step(),
        observation_png=b"png",
        pool=pool,
    )

    assert future == "future"
    assert pool.submitted == (
        action_exec.verify_target,
        (b"png", 10.0, 20.0, "点击确认按钮"),
    )


def test_cdp_no_effect_is_overruled_by_visual_change(monkeypatch):
    monkeypatch.setattr(action_exec, "frame_changed", lambda *_args, **_kwargs: True)

    elapsed, no_effect = action_exec.settle_after_action(
        _CdpPlatform(b"after"),
        b"before",
        "tap",
        center=(10, 20),
    )

    assert elapsed == 0.4
    assert no_effect is False


def test_no_effect_requires_cdp_and_visual_channels_to_agree(monkeypatch):
    monkeypatch.setattr(action_exec, "frame_changed", lambda *_args, **_kwargs: False)

    _, no_effect = action_exec.settle_after_action(
        _CdpPlatform(b"same"),
        b"same",
        "tap",
    )

    assert no_effect is True


def test_finalize_auto_continue_turn_reuses_branch_settle():
    turn = SimpleNamespace(settle_s=None, no_effect=False, target_verify=None)
    verify = TargetVerify(on_target=False, actual_element="取消按钮")
    future = _Future(verify)
    messages = []

    action_exec.finalize_auto_continue_turn(
        turn=turn,
        branch_settle_s=0.4,
        action_decision=None,
        platform=object(),
        observation_png=b"png",
        verify_future=future,
        say=messages.append,
    )

    assert turn.settle_s == 0.4
    assert turn.no_effect is False
    assert turn.target_verify is verify
    assert future.timeout == action_exec.VERIFY_TIMEOUT_S
    assert messages == ["  [TargetVerify] off_target：标记落在「取消按钮」"]


def test_finalize_auto_continue_turn_passes_tap_center(monkeypatch):
    turn = SimpleNamespace(settle_s=None, no_effect=False, target_verify=None)
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)
    captured = {}

    def fake_settle(platform, png, action_type, focus_y=None, *, center=None):
        captured.update({
            "platform": platform,
            "png": png,
            "action_type": action_type,
            "focus_y": focus_y,
            "center": center,
        })
        return 1.2, True

    monkeypatch.setattr(action_exec, "settle_after_action", fake_settle)
    platform = object()

    action_exec.finalize_auto_continue_turn(
        turn=turn,
        branch_settle_s=None,
        action_decision=decision,
        platform=platform,
        observation_png=b"png",
        verify_future=None,
        say=lambda _message: None,
    )

    assert captured == {
        "platform": platform,
        "png": b"png",
        "action_type": "tap",
        "focus_y": None,
        "center": (10.0, 20.0),
    }
    assert turn.settle_s == 1.2
    assert turn.no_effect is True
