from dataclasses import dataclass

from typing import Literal, Optional

from pydantic import BaseModel, Field

from gui_agent.core.schemas import CollectionScope, Milestone


@dataclass(frozen=True)
class MilestonePrompts:
    """Platform-specific LLM prompt set for the milestone supervisor.

    The supervisor FRAMEWORK (policy.py decompose→check→plan loop + helpers) is
    platform-neutral; the actual prompts are iphone-flavored ("你是 iPhone…返回
    主屏幕…底部 Tab") and must be INJECTED per platform — iphone provides
    ``adapters/iphone/supervisor/milestone/prompts.py:IPHONE_MILESTONE_PROMPTS``;
    browser will provide a web-tuned set (today it borrows iphone's). This container
    is the neutral seam: only the field SHAPE lives in core, never the content.
    Fields cover every prompt policy.py + helpers.py consume."""

    decompose: str
    single_checker: str
    check_kind_sections: dict
    check_section_default: str
    check_section_converge: str
    loop_frame: str
    plan: str
    loop_scroll: str
    replan: str
    stop_condition_patch: str


class _SingleCheckResult(BaseModel):
    """Checker output for single-step milestones (navigation/filter/action/verification/read_once).

    LLM checker should only return done or in_progress.
    stuck is reserved for programmatic checks (screen similarity, instruction repetition).
    """
    status: Literal["done", "in_progress", "stuck"] = Field(
        description="判断状态：done（验收通过）或 in_progress（未完成）。禁止填 'loading'——页面加载状态用独立的 loading 布尔字段表示"
    )
    reason: str = Field(description="判断理由")
    stuck_reason: str = Field(default="", description="卡住原因（仅程序化 stuck 时填写）")
    issues: list[str] = Field(default_factory=list)
    visible_evidence: list[str] = Field(default_factory=list, description="截图中支持 done 的可见证据")
    missing_evidence: list[str] = Field(default_factory=list, description="缺失的验收证据")
    page_identity: str = Field(default="", description="当前页面的身份识别（如：订单列表、发票管理、个人中心）")
    summary: str = Field(description="当前屏幕状态一句话描述")
    read_instruction: Optional[str] = Field(
        default=None,
        description="kind=collection(read_once) 或 kind=verification 时填写：当前屏幕需要提取的内容说明；其他类型留空",
    )
    frozen: bool = Field(default=False, description="屏幕是否冻结（相似度≥99%，即使 reader 返回新内容也应停止）")
    loading: bool = Field(default=False, description="页面正在加载（骨架屏/启动屏/转场动画），应等待下一帧而非立即规划动作")


class _LoopFrameResult(BaseModel):
    """Per-frame assessment for scroll_until_boundary milestones."""
    loading: bool = Field(default=False, description="页面尚未稳定渲染（加载中/骨架屏/旧内容未刷新），本帧不应作为采集内容读取")
    boundary_reached: bool = Field(default=False, description="当前可见内容是否已到达列表物理边界（无更多条目）")
    should_stop: bool = Field(default=False, description="是否满足停止条件，应结束滚动采集")
    stop_reason: str = Field(default="", description="停止原因（should_stop=true 时填写）")
    read_instruction: str = Field(default="", description="当前屏幕需要提取的内容说明；无相关内容时留空")
    collection_scope: Optional[CollectionScope] = Field(default=None)
    summary: str = Field(description="当前屏幕内容一句话描述")


class _PlanResult(BaseModel):
    instruction: str = Field(description="下一步精确操作指令")
    summary: str = Field(description="规划依据一句话摘要")
    direction: Optional[Literal["up", "down", "left", "right", "increase", "decrease"]] = Field(
        default=None,
        description=(
            "scroll 时填手指移动方向（up/down/left/right）；"
            "picker drag 时填值的变化方向（increase=值变大，decrease=值变小）；"
            "tap/type/home/stop 留空"
        ),
    )
    # iOS picker-wheel fields (the only iphone-specific part of these otherwise
    # platform-neutral schemas): year/month/day column drag. Neutral platforms
    # (browser) have no such UI and simply leave them None.
    drag_column: Optional[str] = Field(
        default=None,
        description="picker drag 时的目标列，如 'year'/'month'/'day'；非 picker drag 留空",
    )
    drag_current_value: Optional[int] = Field(
        default=None,
        description=(
            "picker drag 时，要拖的【那一列】当前停在中间行的数字（从 check_reason 读出，"
            "如日列当前为 5月1日就填 1、月列当前 6月就填 6）；非 picker drag 留空。"
            "它与 drag_target_value 一起让系统按差几格自动放大拖动幅度——少填会退化成一格一格挪。"
        ),
    )
    drag_target_value: Optional[int] = Field(
        default=None,
        description=(
            "picker drag 时，要拖的【那一列】的目标数字（如目标 5月21日、本步拖日列就填 21）；"
            "非 picker drag 留空。必须与 drag_current_value 取同一列的数字。"
        ),
    )


class _ReplanResult(BaseModel):
    diagnosis: str = Field(description="失败根本原因（一句话）")
    strategy: Literal["local_replan", "escalate_human", "force_complete"]
    instruction: str = Field(default="")
    escalation_message: str = Field(default="")
    can_degrade_to_collection: bool = Field(default=False)


class _StopConditionPatch(BaseModel):
    scroll_stop_condition: str = Field(
        description="一句话描述何时应停止滚动。从依赖链的约束维度推导：有日期范围用日期边界，"
                    "有关键词用关键词消失条件，没有任何约束的全量采集用'滚动至列表物理底部时停止'"
    )
    observable_boundary: bool = Field(
        description="该停止条件是否在屏幕上可直接观察。日期标记、'没有更多了'提示为 true；"
                    "关键词相关性、内容充分性判断为 false"
    )


class _DecomposeResponse(BaseModel):
    goal: str
    global_constraints: list[str] = Field(default_factory=list)
    milestones: list[Milestone]
    task_type: Literal["action", "analysis"] = Field(
        description="action=执行具体操作；analysis=查看/比较/总结信息；有疑问时选 analysis"
    )
