"""Deterministic guards for Android wheel-picker action normalization."""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageDraw

from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
from gui_agent.adapters.android.supervisor.milestone.prompts import ANDROID_MILESTONE_PROMPTS
from gui_agent.adapters.iphone.supervisor.milestone.prompts import IPHONE_MILESTONE_PROMPTS
from gui_agent.adapters.android.policies import AndroidActionPolicy
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.helpers import _build_msgs
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.run.progress_monitor import ProgressMonitor
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def _decision(action: AndroidAction) -> AndroidActionDecision:
    return AndroidActionDecision(action=action)


def _png() -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (80, 120), "white")
    ImageDraw.Draw(img).rectangle([20, 20, 60, 90], fill="black")
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_size(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _message_image_size(msgs) -> tuple[int, int]:
    url = msgs[1].content[1]["image_url"]["url"]
    data = base64.b64decode(url.split(",", 1)[1])
    img = Image.open(io.BytesIO(data))
    return img.size


def test_android_milestone_messages_do_not_half_downscaled_android_frames():
    msgs = _build_msgs(
        "system",
        _png_size(320, 711),
        image_resize=ANDROID_MILESTONE_PROMPTS.image_resize,
    )

    assert _message_image_size(msgs) == (320, 711)


def test_iphone_milestone_messages_still_downscale_retina_frames():
    msgs = _build_msgs(
        "system",
        _png_size(636, 1402),
        image_resize=IPHONE_MILESTONE_PROMPTS.image_resize,
    )

    assert _message_image_size(msgs) == (318, 701)


def test_android_picker_hint_forces_far_minute_column_direction_anchor_and_large_amount():
    policy = AndroidActionPolicy()
    decision = _decision(AndroidAction(action_type="tap", x=100, y=900, description="误点分钟"))

    result = policy._postprocess(
        decision,
        "在分钟列滚动，把 52 分钟调到 30 分钟",
        direction="decrease",
        drag_column="minute",
        drag_steps=22,
    )
    action = result.action

    assert action.action_type == "scroll"
    assert action.direction == "up"
    assert action.amount == "large"
    assert action.x == 765.0
    assert action.y == 240.0
    assert action.snap == {"picker_column": "minute"}


def test_android_picker_mid_minute_uses_medium_amount():
    policy = AndroidActionPolicy()
    decision = _decision(AndroidAction(action_type="scroll", direction="down", amount="small"))

    result = policy._postprocess(
        decision,
        "在分钟列滚动，把 25 分钟调到 30 分钟",
        direction="increase",
        drag_column="minute",
        drag_steps=5,
    )
    action = result.action

    assert action.action_type == "scroll"
    assert action.direction == "down"
    assert action.amount == "medium"
    assert action.x == 765.0
    assert action.y == 240.0


def test_android_picker_hint_forces_hour_increase_to_small_scroll_down():
    policy = AndroidActionPolicy()
    decision = _decision(
        AndroidAction(
            action_type="scroll",
            direction="up",
            amount="large",
            x=100,
            y=900,
            description="调整小时",
        )
    )

    result = policy._postprocess(
        decision,
        "在小时列滚动一点，把 5 点调到 6 点",
        direction="increase",
        drag_column="hour",
        drag_steps=1,
    )
    action = result.action

    assert action.action_type == "scroll"
    assert action.direction == "down"
    assert action.amount == "small"
    assert action.x == 500.0
    assert action.y == 240.0
    assert action.snap == {"picker_column": "hour"}


def test_android_picker_far_hour_uses_medium_but_three_step_stays_small():
    policy = AndroidActionPolicy()
    far = _decision(AndroidAction(action_type="scroll", direction="up", amount="small"))
    near = _decision(AndroidAction(action_type="scroll", direction="up", amount="large"))

    far_result = policy._postprocess(
        far,
        "在小时列滚动，把 11 点调到 6 点",
        direction="decrease",
        drag_column="hour",
        drag_steps=5,
    )
    near_result = policy._postprocess(
        near,
        "在小时列滚动，把 09 点调到 06 点",
        direction="decrease",
        drag_column="hour",
        drag_steps=3,
    )

    assert far_result.action.amount == "medium"
    assert far_result.action.direction == "up"
    assert near_result.action.amount == "small"
    assert near_result.action.direction == "up"


def test_android_picker_fallback_parses_from_to_instruction_without_hints():
    policy = AndroidActionPolicy()
    decision = _decision(AndroidAction(action_type="tap", x=500, y=240, description="调整小时"))

    result = policy._postprocess(decision, "在小时列把小时从 8 调整到 6")
    action = result.action

    assert action.action_type == "scroll"
    assert action.direction == "up"
    assert action.amount == "small"
    assert action.x == 500.0
    assert action.y == 240.0


def test_android_picker_zero_step_hint_does_not_force_picker_scroll():
    policy = AndroidActionPolicy()
    decision = _decision(AndroidAction(action_type="tap", x=950, y=80, description="点击保存"))

    result = policy._postprocess(
        decision,
        "在分钟列向上滚动，将值从 30 调整到 30",
        direction="increase",
        drag_column="minute",
        drag_steps=0,
    )

    assert result.action.action_type == "tap"
    assert result.action.x == 950
    assert result.action.y == 80
    assert result.action.snap is None


def test_android_picker_postprocess_does_not_rewrite_plain_tap():
    policy = AndroidActionPolicy()
    decision = _decision(AndroidAction(action_type="tap", x=880, y=420, description="点击重复设置项"))

    result = policy._postprocess(decision, "点击『重复』设置项")

    assert result.action.action_type == "tap"
    assert result.action.x == 880
    assert result.action.y == 420


def test_android_tap_only_instruction_gets_no_scroll_hint():
    policy = AndroidActionPolicy()

    user_text = policy._build_user_text("点击弹窗列表中可见的 1 week 文字/整行")

    assert "必须输出 action_type=tap" in user_text
    assert "填写目标中心 x/y" in user_text
    assert "没有 x/y 的 tap 会执行失败" in user_text
    assert "禁止改成 scroll/drag" in user_text
    assert "Picker 必须输出 action_type=scroll" not in user_text


def test_android_picker_instruction_keeps_scroll_hint_over_tap_hint():
    policy = AndroidActionPolicy()

    user_text = policy._build_user_text(
        "在分钟列滚动一点，把 5 分钟调到 6 分钟",
        direction="increase",
        drag_column="minute",
        drag_steps=1,
    )

    assert "Picker 必须输出 action_type=scroll" in user_text
    assert "必须输出 action_type=tap" not in user_text


def test_android_picker_postprocess_does_not_rewrite_plain_scroll_with_time_text():
    policy = AndroidActionPolicy()
    decision = _decision(
        AndroidAction(
            action_type="scroll",
            direction="up",
            x=500,
            y=700,
            description="滚动列表",
        )
    )

    result = policy._postprocess(decision, "滚动查看更多最近 30 分钟记录", direction="down")

    assert result.action.action_type == "scroll"
    assert result.action.direction == "down"
    assert result.action.x == 500
    assert result.action.y == 700


def test_android_picker_drag_steps_use_circular_minute_direction():
    plan = _PlanResult(
        instruction="在分钟列滚动，把 58 分钟调到 02 分钟",
        summary="x",
        drag_column="minute",
        drag_current_value=58,
        drag_target_value=2,
    )

    assert MilestoneSupervisorPolicy._picker_drag_steps(plan) == 4
    assert plan.direction == "increase"


def test_android_picker_drag_steps_use_shortest_hour_direction():
    plan = _PlanResult(
        instruction="在小时列滚动，把 9 点调到 6 点",
        summary="x",
        drag_column="hour",
        drag_current_value=9,
        drag_target_value=6,
    )

    assert MilestoneSupervisorPolicy._picker_drag_steps(plan) == 3
    assert plan.direction == "decrease"


def test_zero_step_picker_plan_is_retried_before_action(monkeypatch):
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m4",
        name="确认上午时段并保存",
        description="确保闹钟设定为上午，然后点击保存按钮完成创建。",
        success_condition="闹钟列表中出现 06:30 AM 闹钟条目",
        kind="action",
        completion_strategy="visible_once",
    )
    check = _SingleCheckResult(
        status="in_progress",
        reason="当前时间已设定为 06:30 AM，但尚未点击保存按钮。",
        summary="时间已到位，等待保存。",
    )
    plans = iter(
        [
            _PlanResult(
                instruction="在分钟列向上滚动，将值从 30 调整到 30",
                summary="错误微调",
                direction="increase",
                drag_column="minute",
                drag_current_value=30,
                drag_target_value=30,
            ),
            _PlanResult(instruction="点击右上角的保存按钮", summary="保存闹钟"),
        ]
    )
    extras: list[str] = []

    def fake_invoke_planner(*args, extra: str = "", **kwargs):
        extras.append(extra)
        return next(plans)

    monkeypatch.setattr(policy, "_invoke_planner", fake_invoke_planner)

    step = policy._plan_single(milestone, check, Observation(png_bytes=b"png", source="test"), [])

    assert step.instruction == "点击右上角的保存按钮"
    assert step.drag_column is None
    assert step.drag_steps is None
    assert any("steps=0" in extra for extra in extras)


def test_alarm_time_value_milestone_defaults_to_converge_strategy():
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m2",
        name="设置闹钟时间",
        description="在闹钟页面添加新闹钟，并将时间设置为上午06:30。",
        success_condition="闹钟的时间显示为06:30且AM标识已选中。",
        kind="action",
    )
    policy._milestones = {"m2": milestone}
    policy._order = ["m2"]

    policy._patch_decomposition(None, "创建一个上午6点30的闹钟")  # type: ignore[arg-type]

    assert milestone.completion_strategy == "repeat_until_satisfied"


def test_alarm_goal_period_is_preserved_after_decompose_patch():
    policy = MilestoneSupervisorPolicy()
    time_milestone = Milestone(
        id="m3",
        name="设置闹钟时间为6:30",
        description="使用滚轮选择器将小时设置为“6”，分钟设置为“30”。",
        success_condition="时间选择器上显示的时间为 06:30。",
        kind="action",
        completion_strategy="repeat_until_satisfied",
    )
    save_milestone = Milestone(
        id="m4",
        name="确认并保存闹钟",
        description="点击确定/保存按钮以创建该闹钟，并返回到闹钟列表页面。",
        success_condition="返回到闹钟列表页面，并且列表中新增了设定的06:30的闹钟条目。",
        kind="action",
        completion_strategy="visible_once",
    )
    policy._milestones = {"m3": time_milestone, "m4": save_milestone}
    policy._order = ["m3", "m4"]

    policy._patch_decomposition(None, "创建一个上午6点30的闹钟")  # type: ignore[arg-type]

    assert any("上午/早上/AM" in c for c in policy._global_constraints)
    assert "上午/早上/AM" in time_milestone.description
    assert "上午/早上/AM" in time_milestone.success_condition
    assert "下午/晚上/傍晚/PM" in time_milestone.success_condition
    assert "上午/早上/AM" in save_milestone.success_condition
    assert "下午/晚上/傍晚/PM" in save_milestone.success_condition


def test_goal_period_patch_applies_to_non_alarm_clock_time_targets():
    policy = MilestoneSupervisorPolicy()
    time_milestone = Milestone(
        id="m2",
        name="设置提醒时间为9:15",
        description="将时间设置为 9:15。",
        success_condition="时间显示为 9:15。",
        kind="action",
        completion_strategy="repeat_until_satisfied",
    )
    policy._milestones = {"m2": time_milestone}
    policy._order = ["m2"]

    policy._patch_decomposition(None, "创建一个下午9点15的提醒")  # type: ignore[arg-type]

    assert any("下午/晚上/傍晚/PM" in c for c in policy._global_constraints)
    assert "下午/晚上/傍晚/PM" in time_milestone.description
    assert "下午/晚上/傍晚/PM" in time_milestone.success_condition


def test_goal_repeat_rule_is_preserved_after_decompose_patch():
    policy = MilestoneSupervisorPolicy()
    time_milestone = Milestone(
        id="m2",
        name="设置闹钟时间为6:30",
        description="将时间设置为 6:30。",
        success_condition="时间显示为 6:30。",
        kind="action",
        completion_strategy="repeat_until_satisfied",
    )
    save_milestone = Milestone(
        id="m3",
        name="保存闹钟",
        description="点击保存并返回列表。",
        success_condition="列表中出现 6:30 的闹钟条目。",
        kind="action",
    )
    policy._milestones = {"m2": time_milestone, "m3": save_milestone}
    policy._order = ["m2", "m3"]

    policy._patch_decomposition(None, "创建一个工作日上午6点30的闹钟")  # type: ignore[arg-type]

    assert any("重复规则" in c and "工作日/周一至周五" in c for c in policy._global_constraints)
    assert "重复规则=工作日/周一至周五" in time_milestone.success_condition
    assert "重复规则=工作日/周一至周五" in save_milestone.success_condition


def test_goal_name_field_is_preserved_after_decompose_patch():
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m2",
        name="创建提醒",
        description="创建一个 9:15 的提醒。",
        success_condition="提醒列表中出现 9:15 的提醒。",
        kind="action",
    )
    policy._milestones = {"m2": milestone}
    policy._order = ["m2"]

    policy._patch_decomposition(None, "创建一个下午9点15的提醒，名称设为喝水")  # type: ignore[arg-type]

    assert any("名称/标签" in c and "喝水" in c for c in policy._global_constraints)
    assert "名称/标签=喝水" in milestone.success_condition


def test_iterative_milestone_still_uses_screen_stuck(monkeypatch):
    policy = MilestoneSupervisorPolicy()
    milestone = Milestone(
        id="m2",
        name="设置闹钟时间",
        description="将闹钟时间设置为 06:30",
        success_condition="时间为 06:30",
        kind="action",
        completion_strategy="repeat_until_satisfied",
    )
    check = _SingleCheckResult(
        status="in_progress",
        reason="当前中间行仍为 06:51，目标 06:30",
        summary="当前时间为 06:51",
    )
    stuck = _SingleCheckResult(
        status="stuck",
        reason="连续 3 帧局部与全局均无实质变化",
        stuck_reason="动作无效果",
        summary="屏幕连续无变化",
    )
    history = [
        PolicyTurn(
            index=1,
            observation_source="eval",
            supervisor=SupervisorStep(
                should_act=True,
                instruction="在分钟列滚动，把 51 分钟调到 30 分钟",
                stop=False,
                goal_completed=False,
                summary="",
                milestone_id="m2",
            ),
            action_decision=AndroidActionDecision(
                action=AndroidAction(
                    action_type="scroll",
                    direction="up",
                    amount="large",
                    x=765,
                    y=240,
                    description="滚动分钟列",
                )
            ),
            executed=True,
        )
    ]

    monkeypatch.setattr(policy, "_single_check", lambda *args, **kwargs: check)
    monkeypatch.setattr(policy._monitor, "check_screen_similarity", lambda *args, **kwargs: stuck)
    monkeypatch.setattr(
        policy,
        "_handle_stuck",
        lambda *args, **kwargs: SupervisorStep(
            should_act=False,
            stop=True,
            goal_completed=False,
            stop_reason="stuck",
            summary="stuck handled",
            milestone_id="m2",
        ),
    )

    step = policy._run_single_turn(milestone, Observation(png_bytes=_png(), source="eval"), history)

    assert step.stop is True
    assert step.stop_reason == "stuck"


def test_progress_value_extracts_time_from_reason_without_missing_evidence():
    check = _SingleCheckResult(
        status="in_progress",
        reason="当前页面为新建闹钟界面，时间设定为上午08:51，尚未达到目标时间06:30。",
        summary="当前屏幕为新建闹钟界面。",
    )

    assert ProgressMonitor._extract_progress_value(check) == "上午08:51"
