from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

from gui_agent.core.run.action_exec import ActionExecutor
from gui_agent.core.policies.base import _format_semantic_action_context
from gui_agent.core.schemas import BaseAction, BaseActionDecision, Observation, SupervisorStep


class _Future:
    def __init__(self) -> None:
        self.waited = False

    def result(self):
        self.waited = True


class _Executor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def execute(
        self,
        action_decision,
        *,
        app_name="",
        png_bytes=None,
        is_home_screen=False,
        target_control="",
    ):
        self.calls.append({
            "decision": action_decision,
            "app_name": app_name,
            "png_bytes": png_bytes,
            "is_home_screen": is_home_screen,
            "target_control": target_control,
        })
        return True


class _SnappingExecutor(_Executor):
    def execute(
        self,
        action_decision,
        *,
        app_name="",
        png_bytes=None,
        is_home_screen=False,
        target_control="",
    ):
        ok = super().execute(
            action_decision,
            app_name=app_name,
            png_bytes=png_bytes,
            is_home_screen=is_home_screen,
            target_control=target_control,
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


def _step(decision: BaseActionDecision | None = None) -> SupervisorStep:
    return SupervisorStep(action_intent=ActionIntent(instruction='点击确认按钮'), summary='需要点击', preformed_action=decision, app_name='Settings', is_home_screen=True)


def test_action_policy_context_keeps_semantic_target_and_expected_change() -> None:
    context = _format_semantic_action_context(
        action_family="activate",
        target_control="Search",
        expected_result="the local results grid refreshes with the current filter",
    )

    assert "operation: activate" in context
    assert "target: Search" in context
    assert "expected visible result: the local results grid refreshes" in context
    assert "不得改变指令目标" in context


def test_preformed_action_waits_for_prep_and_executes(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)
    future = _Future()
    executor = _Executor()
    flashes = []
    statuses = []

    result = ActionExecutor().run(
        sv_step=_step(decision),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
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
    assert flashes == [action]
    assert statuses == [(3, "[点击] 点击确认")]
    assert executor.calls == [{
        "decision": decision,
        "app_name": "Settings",
        "png_bytes": b"png",
        "is_home_screen": True,
        "target_control": "",
    }]


def test_preformed_action_reflashes_after_executor_snap(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击确认")
    decision = BaseActionDecision(action=action)
    future = _Future()
    executor = _SnappingExecutor()
    flashes = []

    def _flash(a):
        flashes.append((a.x, a.y, a.snap.copy() if a.snap else None))

    result = ActionExecutor().run(
        sv_step=_step(decision),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
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


def test_action_policy_failure_returns_to_statement_without_execute(tmp_path):
    class _FailingPolicy:
        def decide(self, *_args, **_kwargs):
            raise ValueError("cannot ground target")

    future = _Future()
    executor = _Executor()
    statuses = []

    result = ActionExecutor().run(
        sv_step=_step(),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=_FailingPolicy(),
        supervisor=object(),
        executor=executor,
        prep_future=future,
        log_dir=tmp_path,
        turn_no=4,
        flash=lambda _action: None,
        status=lambda turn_no, message: statuses.append((turn_no, message)),
        say=lambda _message: None,
    )

    assert not future.waited
    assert result.action_decision is None
    assert result.executed is False
    assert "could not ground" in result.suppressed_reason
    assert statuses == [
        (4, "动作决策中…"),
        (4, "动作意图未能落成物理动作，交回 Statement 重决策"),
    ]
    assert executor.calls == []


def test_action_policy_failure_replans_and_dispatches_once_on_the_same_frame(tmp_path):
    class _FailingPolicy:
        def decide(self, *_args, **_kwargs):
            raise ValueError("cannot ground target")

    replacement_action = BaseActionDecision(
        action=BaseAction(action_type="tap", x=30, y=40, description="点击网格 Search")
    )
    replacement = _step(replacement_action)
    executor = _Executor()
    replans: list[str] = []

    result = ActionExecutor().run(
        sv_step=_step(),
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=_FailingPolicy(),
        supervisor=object(),
        executor=executor,
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=4,
        flash=lambda _action: None,
        status=lambda _turn_no, _message: None,
        say=lambda _message: None,
        replan=lambda _step, reason: replans.append(reason) or replacement,
    )

    assert len(replans) == 1
    assert result.supervisor_step is replacement
    assert result.executed is True
    assert result.suppressed_reason == ""
    assert len(executor.calls) == 1
    assert executor.calls[0]["decision"] is replacement_action


def test_executor_does_not_apply_legacy_commit_suppression(tmp_path):
    action = BaseAction(action_type="tap", x=10, y=20, description="点击保存")
    decision = BaseActionDecision(action=action)
    step = _step(decision).model_copy(update={
        "action_intent": _step(decision).action_intent.model_copy(
            update={"role": "commit"}
        ),
        "statement_id": "m1",
    })
    executor = _Executor()

    result = ActionExecutor().run(
        sv_step=step,
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=_DenyingSupervisor(),
        executor=executor,
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
    step = _step(decision).model_copy(update={
        "action_intent": _step(decision).action_intent.model_copy(
            update={"family": "input"}
        )
    })
    executor = _Executor()

    result = ActionExecutor().run(
        sv_step=step,
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
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


def test_target_directed_iterate_scroll_dispatches_exactly_once(tmp_path):
    action = BaseAction(
        action_type="scroll",
        direction="down",
        amount="medium",
        description="滚动到目标",
    )
    decision = BaseActionDecision(action=action)
    step = _step(decision).model_copy(update={
        "action_intent": ActionIntent(
            instruction="滚动到目标",
            role="iterate",
            family="iterate",
            target_control="Details",
            direction="down",
        ),
        "statement_id": "m-acquire",
        "execution_scope": "row:42",
    })
    executor = _Executor()
    result = ActionExecutor().run(
        sv_step=step,
        observation=Observation(png_bytes=b"png", source="test"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=7,
        flash=lambda _action: None,
        status=lambda _turn_no, _message: None,
        say=lambda _message: None,
    )

    assert result.executed is True
    assert result.action_role == "iterate"
    assert result.action_key.endswith("|iterate|scroll|down|@-")
    assert len(executor.calls) == 1
    assert executor.calls[0]["decision"].action == action
