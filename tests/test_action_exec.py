from __future__ import annotations

from gui_agent.core.run.action_exec import ActionExecutionState
from gui_agent.core.schemas import BaseAction, BaseActionDecision, Observation, SupervisorStep


class _Future:
    def __init__(self) -> None:
        self.waited = False

    def result(self):
        self.waited = True


class _Executor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(self, action_decision, *, app_name="", png_bytes=None, is_home_screen=False):
        self.calls.append({
            "decision": action_decision,
            "app_name": app_name,
            "png_bytes": png_bytes,
            "is_home_screen": is_home_screen,
        })
        return True


class _SnappingExecutor(_Executor):
    def execute(self, action_decision, *, app_name="", png_bytes=None, is_home_screen=False):
        ok = super().execute(
            action_decision,
            app_name=app_name,
            png_bytes=png_bytes,
            is_home_screen=is_home_screen,
        )
        action_decision.action.snap = {
            "method": "dom",
            "original": [10, 20],
            "snapped": [30, 40],
        }
        return ok


def _step(decision: BaseActionDecision) -> SupervisorStep:
    return SupervisorStep(
        should_act=True,
        instruction="点击确认按钮",
        stop=False,
        goal_completed=False,
        summary="需要点击",
        preformed_action=decision,
        app_name="Settings",
        is_home_screen=True,
    )


def test_preformed_action_waits_for_prep_and_executes(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)
    future = _Future()
    executor = _Executor()
    flashes = []
    statuses = []

    result = ActionExecutionState().run(
        sv_step=_step(decision),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=object(),
        platform=object(),
        prep_future=future,
        log_dir=tmp_path,
        turn_no=3,
        flash=flashes.append,
        status=lambda turn_no, message: statuses.append((turn_no, message)),
        say=lambda _message: None,
    )

    assert future.waited
    assert result.action_decision is decision
    assert result.executed is True
    assert result.probe_failed is False
    assert flashes == [action]
    assert statuses == [(3, "[点击] 点击确认")]
    assert executor.calls == [{
        "decision": decision,
        "app_name": "Settings",
        "png_bytes": b"png",
        "is_home_screen": True,
    }]


def test_preformed_action_reflashes_after_executor_snap(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)
    future = _Future()
    executor = _SnappingExecutor()
    flashes = []

    def _flash(a):
        flashes.append((a.x, a.y, a.snap.copy() if a.snap else None))

    result = ActionExecutionState().run(
        sv_step=_step(decision),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=object(),
        platform=object(),
        prep_future=future,
        log_dir=tmp_path,
        turn_no=3,
        flash=_flash,
        status=lambda _turn_no, _message: None,
        say=lambda _message: None,
    )

    assert result.executed is True
    assert flashes == [
        (10, 20, None),
        (10, 20, {"method": "dom", "original": [10, 20], "snapped": [30, 40]}),
    ]


def test_not_found_waits_for_prep_and_skips_execute(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action, not_found_reason="未找到确认按钮")
    future = _Future()
    executor = _Executor()
    statuses = []

    result = ActionExecutionState().run(
        sv_step=_step(decision),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=object(),
        platform=object(),
        prep_future=future,
        log_dir=tmp_path,
        turn_no=4,
        flash=lambda _action: None,
        status=lambda turn_no, message: statuses.append((turn_no, message)),
        say=lambda _message: None,
    )

    assert future.waited
    assert result.action_decision is decision
    assert result.executed is False
    assert result.probe_failed is False
    assert statuses == [(4, "未找到目标元素")]
    assert executor.calls == []
