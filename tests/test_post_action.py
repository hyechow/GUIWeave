from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run import post_action
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    SupervisorStep,
    TargetVerify,
)


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

    future = post_action.submit_target_verify(
        action_decision=decision,
        executed=True,
        sv_step=_step(),
        observation_png=b"png",
        pool=pool,
    )

    assert future == "future"
    assert pool.submitted == (
        post_action.verify_target,
        (b"png", 10.0, 20.0, "点击确认按钮"),
    )


def test_finalize_auto_continue_turn_reuses_branch_settle():
    turn = SimpleNamespace(settle_s=None, no_effect=False, target_verify=None)
    verify = TargetVerify(on_target=False, actual_element="取消按钮")
    future = _Future(verify)
    messages = []

    post_action.finalize_auto_continue_turn(
        turn=turn,
        branch_settle_s=0.4,
        action_decision=None,
        phone=object(),
        observation_png=b"png",
        verify_future=future,
        say=messages.append,
    )

    assert turn.settle_s == 0.4
    assert turn.no_effect is False
    assert turn.target_verify is verify
    assert future.timeout == post_action.VERIFY_TIMEOUT_S
    assert messages == ["  [TargetVerify] off_target：标记落在「取消按钮」"]


def test_finalize_auto_continue_turn_passes_tap_center(monkeypatch):
    turn = SimpleNamespace(settle_s=None, no_effect=False, target_verify=None)
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)
    captured = {}

    def fake_settle(phone, png, action_type, focus_y=None, *, center=None):
        captured.update({
            "phone": phone,
            "png": png,
            "action_type": action_type,
            "focus_y": focus_y,
            "center": center,
        })
        return 1.2, True

    monkeypatch.setattr(post_action, "settle_after_action", fake_settle)
    phone = object()

    post_action.finalize_auto_continue_turn(
        turn=turn,
        branch_settle_s=None,
        action_decision=decision,
        phone=phone,
        observation_png=b"png",
        verify_future=None,
        say=lambda _message: None,
    )

    assert captured == {
        "phone": phone,
        "png": b"png",
        "action_type": "tap",
        "focus_y": None,
        "center": (10.0, 20.0),
    }
    assert turn.settle_s == 1.2
    assert turn.no_effect is True
