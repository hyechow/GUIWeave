"""Deterministic guards for Android wheel-picker action normalization."""

from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

import base64
import io

from PIL import Image, ImageDraw

from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
from gui_agent.adapters.android.supervisor.statement.prompts import ANDROID_STATEMENT_PROMPTS
from gui_agent.adapters.iphone.supervisor.statement.prompts import IPHONE_STATEMENT_PROMPTS
from gui_agent.adapters.android.policies import AndroidActionPolicy
from gui_agent.core.schemas import (
    StatementContract,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.orchestrator.passes import normalize_goal_value_contracts
from gui_agent.core.orchestrator.program import Program, Run
from gui_agent.core.run.interactive import contract_for_run
from gui_agent.core.supervisor.statement.model_io import _build_msgs
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _ActionDraft,
    _StatementTransitionResult,
    _TransitionAction,
)


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


def test_android_statement_messages_do_not_half_downscaled_android_frames():
    msgs = _build_msgs(
        "system",
        _png_size(320, 711),
        image_resize=ANDROID_STATEMENT_PROMPTS.image_resize,
    )

    assert _message_image_size(msgs) == (320, 711)


def test_iphone_statement_messages_still_downscale_retina_frames():
    msgs = _build_msgs(
        "system",
        _png_size(636, 1402),
        image_resize=IPHONE_STATEMENT_PROMPTS.image_resize,
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
    plan = _ActionDraft(
        instruction="在分钟列滚动，把 58 分钟调到 02 分钟",
        summary="x",
        drag_column="minute",
        drag_current_value=58,
        drag_target_value=2,
    )

    assert StatementSupervisorPolicy._picker_drag_steps(plan) == 4
    assert plan.direction == "increase"


def test_android_picker_drag_steps_use_shortest_hour_direction():
    plan = _ActionDraft(
        instruction="在小时列滚动，把 9 点调到 6 点",
        summary="x",
        drag_column="hour",
        drag_current_value=9,
        drag_target_value=6,
    )

    assert StatementSupervisorPolicy._picker_drag_steps(plan) == 3
    assert plan.direction == "decrease"


def test_zero_step_picker_transition_is_redecided_before_action(monkeypatch):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m4",
        name="确认上午时段并保存",
        description="确保闹钟设定为上午，然后点击保存按钮完成创建。",
        success_condition="闹钟列表中出现 06:30 AM 闹钟条目",
        kind="action",
        completion_strategy="visible_once",
    )
    policy.begin_statement(statement, instance_id="i1")
    decisions = iter(
        [
            _StatementTransitionResult(
                kind="act",
                reason="错误微调",
                summary="错误微调",
                action=_TransitionAction(
                    instruction="在分钟列向上滚动，将值从 30 调整到 30",
                    direction="increase",
                    drag_column="minute",
                    drag_current_value=30,
                    drag_target_value=30,
                ),
            ),
            _StatementTransitionResult(
                kind="act",
                reason="时间已到位，下一步保存",
                summary="保存闹钟",
                action=_TransitionAction(
                    instruction="点击右上角的保存按钮",
                    atomic_role="commit",
                    action_family="activate",
                ),
            ),
        ]
    )
    extras: list[str] = []

    def fake_transition(*args, extra: str = "", **kwargs):
        extras.append(extra)
        return next(decisions)

    monkeypatch.setattr(policy, "_invoke_statement_transition", fake_transition)
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _observation: False,
    )

    step = policy._run_single_turn(
        statement,
        Observation(png_bytes=_png(), source="test"),
        [],
    )

    assert step.action_intent.instruction == "点击右上角的保存按钮"
    assert step.action_intent.drag_column is None
    assert step.action_intent.drag_steps is None
    assert any("already at its target" in extra for extra in extras)


def test_alarm_time_value_statement_defaults_to_converge_strategy():
    run = Run(
        name="设置闹钟时间",
        success_condition="闹钟的时间显示为06:30且AM标识已选中。",
        kind="action",
    )
    assert contract_for_run(run, 0).completion_strategy == "repeat_until_satisfied"


def test_alarm_goal_period_is_preserved_after_decompose_patch():
    time_run = Run(
        name="设置闹钟时间为6:30",
        success_condition="时间选择器上显示的时间为 06:30。",
        kind="action",
    )
    save_run = Run(
        name="确认并保存闹钟",
        success_condition="返回到闹钟列表页面，并且列表中新增了设定的06:30的闹钟条目。",
        kind="action",
    )
    program = normalize_goal_value_contracts(Program(
        goal="创建一个上午6点30的闹钟", statements=[time_run, save_run]
    ))
    first, second = program.statements
    assert "上午/早上/AM" in first.success_condition
    assert "下午/晚上/傍晚/PM" in first.success_condition
    assert "上午/早上/AM" in second.success_condition
    assert "下午/晚上/傍晚/PM" in second.success_condition


def test_goal_period_patch_applies_to_non_alarm_clock_time_targets():
    time_run = Run(
        name="设置提醒时间为9:15",
        success_condition="时间显示为 9:15。",
        kind="action",
    )
    program = normalize_goal_value_contracts(Program(
        goal="创建一个下午9点15的提醒", statements=[time_run]
    ))
    assert "下午/晚上/傍晚/PM" in program.statements[0].success_condition


def test_goal_repeat_rule_is_preserved_after_decompose_patch():
    time_run = Run(
        name="设置闹钟时间为6:30",
        success_condition="时间显示为 6:30。",
        kind="action",
    )
    save_run = Run(
        name="保存闹钟",
        success_condition="列表中出现 6:30 的闹钟条目。",
        kind="action",
    )
    program = normalize_goal_value_contracts(Program(
        goal="创建一个工作日上午6点30的闹钟", statements=[time_run, save_run]
    ))
    assert "重复规则=工作日/周一至周五" in program.statements[0].success_condition
    assert "重复规则=工作日/周一至周五" in program.statements[1].success_condition


def test_goal_name_field_is_preserved_after_decompose_patch():
    run = Run(
        name="创建提醒",
        success_condition="提醒列表中出现 9:15 的提醒。",
        kind="action",
    )
    program = normalize_goal_value_contracts(Program(
        goal="创建一个下午9点15的提醒，名称设为喝水", statements=[run]
    ))
    assert "名称/标签=喝水" in program.statements[0].success_condition


def test_iterative_statement_strategy_change_is_chosen_by_transition(monkeypatch):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="m2",
        name="设置闹钟时间",
        description="将闹钟时间设置为 06:30",
        success_condition="时间为 06:30",
        kind="action",
        completion_strategy="repeat_until_satisfied",
    )
    policy.begin_statement(statement, instance_id="i1")
    history = [
            PolicyTurn(
                index=1,
                observation_source="eval",
                statement_instance_id="i1",
                supervisor=SupervisorStep(action_intent=ActionIntent(instruction='在分钟列滚动，把 51 分钟调到 30 分钟'), summary='', statement_id='m2'),
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

    memory_sections: list[str] = []

    def choose_next(*args, memory_view, **kwargs):
        memory_sections.append(memory_view.render_prompt_section())
        return _StatementTransitionResult(
            kind="act",
            reason="上一滚动未使 06:51 接近 06:30，改用更大幅度",
            summary="换一个连续调整策略",
            action=_TransitionAction(
                instruction="在分钟列向下大幅滚动以接近 30",
                atomic_role="iterate",
                action_family="iterate",
                direction="decrease",
                drag_column="minute",
                drag_current_value=51,
                drag_target_value=30,
            ),
        )

    monkeypatch.setattr(policy, "_invoke_statement_transition", choose_next)

    step = policy._run_single_turn(statement, Observation(png_bytes=_png(), source="eval"), history)

    assert step.outcome is None
    assert step.action_intent is not None
    assert any("51 分钟调到 30" in section for section in memory_sections)
