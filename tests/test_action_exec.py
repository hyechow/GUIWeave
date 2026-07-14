from __future__ import annotations

from types import SimpleNamespace

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
            "snapped": [130, 140],
        }
        return ok


class _DenyingSupervisor:
    def authorize_action_dispatch(self, _step, _decision, _history):
        return False, "scope|m1|commit", "commit already dispatched"


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
    assert result.action_key.endswith("|prepare|tap|@2,2|")
    assert flashes == [
        (10, 20, None),
        (10, 20, {"method": "dom", "original": [10, 20], "snapped": [130, 140]}),
    ]


def test_not_found_waits_for_prep_and_skips_execute(tmp_path):
    decision = BaseActionDecision(action=None, not_found_reason="未找到确认按钮")
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
    assert result.action_decision.action is None
    assert result.executed is False
    assert result.probe_failed is False
    assert statuses == [(4, "未找到目标元素")]
    assert executor.calls == []


def test_executor_does_not_apply_legacy_commit_suppression(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击保存")
    decision = BaseActionDecision(action=action)
    step = _step(decision).model_copy(
        update={"atomic_role": "commit", "milestone_id": "m1"}
    )
    executor = _Executor()

    result = ActionExecutionState().run(
        sv_step=step,
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=_DenyingSupervisor(),
        history=[object()],
        executor=executor,
        bundle=object(),
        platform=object(),
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=5,
        flash=lambda _action: None,
        status=lambda _turn_no, _message: None,
        say=lambda _message: None,
    )

    assert result.executed is True
    assert result.action_role == "commit"
    assert result.action_key == "|m1|commit"
    assert result.suppressed_reason == ""
    assert len(executor.calls) == 1
    assert executor.calls[0]["decision"].action == action


def test_action_family_is_advisory_after_grounding(tmp_path):
    decision = BaseActionDecision(
        action=BaseAction(action_type="tap", x=10, y=20, description="点击输入框")
    )
    step = _step(decision).model_copy(update={"action_family": "input"})
    executor = _Executor()

    result = ActionExecutionState().run(
        sv_step=step,
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=object(),
        platform=object(),
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=6,
        flash=lambda _action: None,
        status=lambda _turn_no, _message: None,
        say=lambda _message: None,
    )

    assert result.executed is True
    assert result.suppressed_reason == ""
    assert len(executor.calls) == 1
    assert executor.calls[0]["decision"].action == decision.action


def test_target_directed_iterate_scroll_dispatches_once_without_boundary_probe(tmp_path):
    action = BaseAction(
        action_type="scroll",
        direction="down",
        amount="medium",
        description="滚动到目标",
    )
    decision = BaseActionDecision(action=action)
    step = _step(decision).model_copy(update={
        "milestone_id": "m-acquire",
        "execution_scope": "row:42",
        "atomic_role": "iterate",
        "action_family": "iterate",
        "target_control": "Details",
        "direction": "down",
        "completion_strategy": "visible_once",
    })
    probe_calls: list[str] = []

    class _Probe:
        def probe(self, _png, proposed, *, turn_no):
            probe_calls.append(f"probe:{turn_no}:{proposed.direction}")
            return SimpleNamespace(
                success=True,
                profile=SimpleNamespace(direction="down"),
                reason="progress",
            )

    class _Bundle:
        @staticmethod
        def make_scroll_probe(_platform, _executor, _log_dir):
            return _Probe()

        @staticmethod
        def apply_scroll_profile(proposed, _profile):
            return proposed

    executor = _Executor()
    result = ActionExecutionState().run(
        sv_step=step,
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        bundle=_Bundle(),
        platform=object(),
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=7,
        flash=lambda _action: None,
        status=lambda _turn_no, _message: None,
        say=lambda _message: None,
    )

    assert result.executed is True
    assert result.probe_failed is False
    assert result.action_role == "iterate"
    assert result.action_key.endswith("|iterate|scroll|down|@-")
    assert probe_calls == []
    assert len(executor.calls) == 1
    assert executor.calls[0]["decision"].action == action
