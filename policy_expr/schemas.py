"""Shared schemas for policy experiments."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


ActionType = Literal["tap", "type", "clear_text", "press_enter", "scroll", "drag", "home", "stop"]
ScrollTargetArea = Literal[
    "main_content",
    "left_panel",
    "right_panel",
    "top_content",
    "bottom_content",
    "picker_left",
    "picker_center",
    "picker_right",
]
ScrollAmount = Literal["small", "medium", "large"]
ScrollMethod = Literal["auto", "wheel", "drag"]
ValueDirection = Literal["increase", "decrease"]

_ACTION_TYPE_LABELS: dict[str, str] = {
    "tap": "点击",
    "type": "输入",
    "clear_text": "清空",
    "press_enter": "回车",
    "scroll": "滚动",
    "drag": "拖动",
    "home": "主屏",
    "stop": "停止",
}


def action_label(action_type: str) -> str:
    return _ACTION_TYPE_LABELS.get(action_type, action_type)
TaskType = Literal["action", "analysis"]
MilestoneKind = Literal["navigation", "filter", "collection", "action", "verification"]
CompletionStrategy = Literal[
    "visible_once",
    "read_once",
    "scroll_until_boundary",
    "repeat_until_satisfied",
    "human_escalation",
]

# 「连续操作」轴（与 kind 正交）：靠重复调整逼近目标的策略，区别于单步达成。
#   - repeat_until_satisfied：收敛到目标值（picker 调日期/时间、步进器、滑块）
#   - scroll_until_boundary：滚动采集直到边界（已有 loop 机制）
# 连续操作的「进展/卡住」判据与单步不同：进展=被监控值朝目标逼近，重复同一操作属正常，
# 故 checker(进展传感器)/planner(收敛分流)/stuck(值停滞判据) 都按此轴分流。
ITERATIVE_STRATEGIES: tuple[str, ...] = ("repeat_until_satisfied", "scroll_until_boundary")


class CollectionScope(BaseModel):
    """Structured scope for collected content."""

    label: str = Field(default="", description="范围标签，如目标范围、当前分组、自定义条件")
    start: Optional[str] = Field(default=None, description="范围开始值；不可确定则为空")
    end: Optional[str] = Field(default=None, description="范围结束值；不可确定则为空")
    evidence: list[str] = Field(default_factory=list, description="截图中支持该范围的可见证据")


class Action(BaseModel):
    """A single phone action in normalized coordinates."""

    @model_validator(mode="before")
    @classmethod
    def _unpack_coords(cls, data: object) -> object:
        """Unpack x: [x, y] into separate x/y fields (model sometimes uses list coords)."""
        if isinstance(data, dict):
            x = data.get("x")
            y = data.get("y")
            if isinstance(x, list) and len(x) == 2:
                data = {**data, "x": x[0]}
                if not isinstance(y, (int, float)):
                    data["y"] = x[1]
            if isinstance(data.get("x"), list) and len(data["x"]) == 1:
                data = {**data, "x": data["x"][0]}
            if isinstance(data.get("y"), list) and len(data["y"]) == 1:
                data = {**data, "y": data["y"][0]}
            # 常见 LLM 别名：click → tap
            if data.get("action_type") == "click":
                data["action_type"] = "tap"
            if not data.get("description"):
                action_type = data.get("action_type") or "操作"
                text = data.get("text")
                if text:
                    data["description"] = f"执行{action_type}并输入{text}"
                else:
                    data["description"] = f"执行{action_type}操作"
            # NOTE: the iPhone status-bar / home-indicator dead-zone clamp that
            # used to live here moved into the iphone executor (S3) — it is a
            # phone-screen concern and must NOT apply to other platforms (it was
            # mis-clicking the top/bottom of web pages on the browser adapter).
        return data

    action_type: ActionType = Field(
        description="操作类型：tap、type、press_enter、clear_text、scroll、drag、home、stop 之一"
    )
    x: Optional[float] = Field(
        default=None,
        description="归一化 x 坐标（0-1000）。tap/type 为目标中心；scroll/drag 为滚动锚点，可为空走 target_area 默认点",
    )
    y: Optional[float] = Field(
        default=None,
        description="归一化 y 坐标（0-1000）。tap/type 为目标中心；scroll/drag 为滚动锚点，可为空走 target_area 默认点",
    )
    direction: Optional[str] = Field(
        default=None,
        description="内容方向：up（查看上方内容）、down（查看下方内容）、left（查看右侧内容）、right（查看左侧内容）。普通列表 scroll/drag 使用",
    )
    value_direction: Optional[ValueDirection] = Field(
        default=None,
        description="数值方向：increase（调大数值）、decrease（调小数值）。picker 滚轮优先使用它，避免和手势方向混淆",
    )
    target_area: ScrollTargetArea = Field(
        default="main_content",
        description="滚动目标区域：main_content/left_panel/right_panel/top_content/bottom_content/picker_left/picker_center/picker_right",
    )
    amount: ScrollAmount = Field(
        default="medium",
        description="滚动幅度：small/medium/large。普通翻看用 medium，细微调整用 small，快速翻页用 large",
    )
    method: ScrollMethod = Field(
        default="auto",
        description="滚动执行方式：auto（运行时自动探测）、wheel（滚轮）、drag（触摸拖动）。普通页面优先 auto，picker 用 drag",
    )
    to_x: Optional[float] = Field(
        default=None,
        description="执行层内部字段：drag 结束点归一化 x 坐标，action policy 不要填写",
    )
    to_y: Optional[float] = Field(
        default=None,
        description="执行层内部字段：drag 结束点归一化 y 坐标，action policy 不要填写",
    )
    duration_ms: Optional[int] = Field(
        default=None,
        description="执行层内部字段：drag 手势持续时间毫秒，action policy 不要填写",
    )
    text: Optional[str] = Field(
        default=None,
        description="要输入的文字内容（action_type 为 type 时必填）",
    )
    description: str = Field(description="该操作的中文说明，如「点击目标应用图标」")
    snap: Optional[dict] = Field(
        default=None,
        description="执行层内部字段：YOLO/OCR 吸附记录，包含 method/original_xy/snapped_xy",
    )

    @model_validator(mode="after")
    def _require_text_for_type(self) -> "Action":
        if self.action_type == "type" and not self.text:
            raise ValueError("type 动作必须填写 text 字段，不能为空")
        if self.action_type in {"scroll", "drag"} and not (self.direction or self.value_direction):
            raise ValueError("scroll/drag 动作必须填写 direction 或 value_direction")
        return self


class Observation(BaseModel):
    """Raw environment observation used by policies."""

    png_bytes: bytes = Field(description="当前 iPhone 截图 PNG bytes")
    source: str = Field(description="观测来源")


class ActionDecision(BaseModel):
    """Action policy output: the next action to execute."""

    action: Action = Field(description="当前应该执行的操作")
    not_found_reason: Optional[str] = Field(
        default=None,
        description="当截图中找不到指令要求的目标元素时填写原因（如「当前页面无通讯录标签」）；找到目标时留空",
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_flat_action(cls, data: object) -> object:
        """Accept flat Action fields directly (model forgot the outer wrapper)."""
        if isinstance(data, dict) and "action_type" in data and "action" not in data:
            return {"action": data}
        return data


class SupervisorStep(BaseModel):
    """Supervisor policy decision for one turn."""

    should_act: bool = Field(description="是否调用 action policy 执行动作")
    instruction: Optional[str] = Field(
        default=None,
        description="给 action policy 的精确操作指令（should_act=true 时必填）",
    )
    stop: bool = Field(description="是否终止 agent loop")
    stop_reason: str = Field(default="", description="终止原因（stop=true 时填写）")
    goal_completed: bool = Field(description="用户目标是否已完全达成")
    app_name: Optional[str] = Field(default=None, description="当前前台应用名称")
    summary: str = Field(description="对当前屏幕状态和任务进展的简要描述")
    preformed_action: Optional[ActionDecision] = Field(
        default=None,
        description="预生成的动作决策（设置后 runner 跳过 Action Policy 直接执行）",
    )
    read_instruction: Optional[str] = Field(
        default=None,
        description="当前屏幕需要提取的内容说明（analysis 任务时由 Checker 填写）",
    )
    allow_read: bool = Field(default=False, description="是否允许 runner 将读取结果写入 content_notes")
    milestone_id: Optional[str] = Field(default=None, description="当前子目标 ID")
    milestone_kind: Optional[MilestoneKind] = Field(default=None, description="当前子目标类型")
    completion_strategy: Optional[CompletionStrategy] = Field(default=None, description="当前子目标完成策略")
    collection_scope: Optional[CollectionScope] = Field(default=None, description="当前内容采集范围")
    pre_existing: bool = Field(
        default=False,
        description="目标完成时，完成该里程碑的 action 由智能体执行（False）还是该状态在本次会话前就已存在（True）",
    )
    collection_summary: Optional[str] = Field(
        default=None,
        description="collection milestone 完成时的采集摘要（含停止条件及触发原因）",
    )
    direction: Optional[str] = Field(default=None, description="scroll/drag 手指方向 hint（up/down/left/right）")
    drag_column: Optional[str] = Field(default=None, description="picker drag 目标列 hint（year/month/day）")
    drag_steps: Optional[int] = Field(
        default=None,
        description="picker drag 目标列当前值与目标值相差的格数（绝对值）hint，用于按距离放大拖动幅度",
    )
    # 由 checker 的 page_identity 在代码中派生（见 _is_home_identity），非 LLM 填写。
    is_home_screen: bool = Field(
        default=False,
        description="当前是否为 iOS 主屏幕（springboard）",
    )
    # 页面未稳定（白屏/加载中）的等待帧：runner 据此跳过本帧、不计入 max_turns、不累加 noop。
    is_loading: bool = Field(
        default=False,
        description="当前帧页面尚未渲染稳定（白屏/加载中），应等待重新观察而非执行/计数",
    )


class GoalValidationResult(BaseModel):
    """Result of independent goal-completion validation."""

    sufficient: bool = Field(description="已收集数据是否充分回答了用户目标")
    missing: str = Field(default="", description="缺少什么（sufficient=false 时填写）")


class Milestone(BaseModel):
    """A sub-goal in the task decomposition DAG."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_kind_and_strategy(cls, data: object) -> object:
        """Normalize common LLM aliases for milestone intent fields."""
        if isinstance(data, dict):
            kind_aliases = {
                "analysis": "verification",
                "analyze": "verification",
                "summary": "verification",
                "summarize": "verification",
                "report": "verification",
                "read": "collection",
                "reading": "collection",
                "collect": "collection",
                "data_collection": "collection",
                "browse": "collection",
                "navigation": "navigation",
                "navigate": "navigation",
                "filtering": "filter",
                "search": "filter",
            }
            strategy_aliases = {
                "scroll": "scroll_until_boundary",
                "scroll_until_end": "scroll_until_boundary",
                "scroll_to_bottom": "scroll_until_boundary",
                "read": "read_once",
                "read_visible": "read_once",
                "once": "visible_once",
                "visible": "visible_once",
                "manual": "human_escalation",
            }
            kind = data.get("kind")
            strategy = data.get("completion_strategy")
            normalized = dict(data)
            if isinstance(kind, str):
                normalized["kind"] = kind_aliases.get(kind.strip().lower(), kind)
            if isinstance(strategy, str):
                normalized["completion_strategy"] = strategy_aliases.get(
                    strategy.strip().lower(), strategy
                )
            return normalized
        return data

    id: str
    name: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    success_condition: str
    kind: MilestoneKind = Field(
        default="action",
        description="navigation | filter | collection | action | verification",
    )
    completion_strategy: CompletionStrategy = Field(
        default="visible_once",
        description=(
            "visible_once | read_once | scroll_until_boundary | "
            "repeat_until_satisfied | human_escalation"
        ),
    )
    scroll_stop_condition: str = Field(
        default="",
        description=(
            "仅 completion_strategy=scroll_until_boundary 时填写。"
            "一句话描述何时应停止滚动，例如："
            "「当可见记录日期早于2026-05-03时停止」"
            "「当可见内容不再包含1星评价时停止」"
            "「滚动至列表物理底部时停止」"
        ),
    )
    observable_boundary: bool = Field(
        default=True,
        description="停止条件是否在屏幕上可直接观察。日期标记、列表结束标识为 true；关键词相关性为 false",
    )
    scroll_budget: int = Field(
        default=0,
        description="滚动预算上限（0=使用系统默认）。筛选降级为全量采集时由系统自动放大。",
    )
    failure_hints: list[str] = Field(default_factory=list)
    status: str = Field(default="pending", description="pending | running | done | failed")
    retry_count: int = 0

    @property
    def is_iterative(self) -> bool:
        """连续操作：靠重复调整逼近目标（picker 调值 / 滚动采集），区别于单步达成。
        驱动 checker/planner/stuck 按「单步 vs 连续」分流。见 ITERATIVE_STRATEGIES。"""
        return self.completion_strategy in ITERATIVE_STRATEGIES

    @property
    def is_converge(self) -> bool:
        """连续操作中的「收敛到目标值」一味（picker/步进器/滑块）：重复同一操作逐步逼近
        success_condition 指定的目标值。区别于 scroll_until_boundary（滚动采集）。"""
        return self.completion_strategy == "repeat_until_satisfied"


class TargetVerify(BaseModel):
    """Post-action targeting verify: did the snapped tap land on the intended element."""

    on_target: bool = Field(description="标记圆环是否正好落在指令意图的目标元素上")
    actual_element: str = Field(default="", description="标记实际落在的元素（如「转账 tab」「搜索框」）")
    reason: str = Field(default="", description="一句话理由")


class PolicyTurn(BaseModel):
    """One observe-decide-act turn saved in continue mode."""

    index: int
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    observation_source: str
    supervisor: SupervisorStep
    action_decision: Optional[ActionDecision] = None
    checker: Optional[dict] = Field(default=None, description="Checker 原始结果：status, reason, summary, missing_evidence 等")
    planner: Optional[dict] = Field(default=None, description="Planner 原始结果：instruction, summary, direction, drag_column")
    replan: Optional[dict] = Field(default=None, description="Replan 原始结果：diagnosis, strategy, instruction")
    executed: bool = False
    llm_calls: int = 0
    input_tokens: int = Field(default=0, description="本轮 LLM 调用累计输入 tokens（与 llm_calls 同口径），用于成本核算")
    output_tokens: int = Field(default=0, description="本轮 LLM 调用累计输出 tokens（与 llm_calls 同口径），用于成本核算")
    read_added_content: bool = False
    read_note_hash: Optional[str] = None
    target_verify: Optional[TargetVerify] = Field(default=None, description="动作后落点校验：on_target, actual_element")
    timings: dict[str, float] = Field(default_factory=dict, description="各模块耗时(秒)，如 {checker: 1.2, planner: 2.3}")
    token_usage: dict[str, dict[str, int]] = Field(default_factory=dict, description="各模块 token 用量，如 {checker: {input: 2284, output: 114}, planner: {...}}")
    settle_s: Optional[float] = Field(default=None, description="本轮动作后 settle 等待时长(秒)，等屏幕变过且停稳")
    no_effect: bool = Field(default=False, description="tap 类动作 settle 跑满上限且全程零变化：这一击对屏幕无效果（如重点已高亮 tab）")


class PolicyContext(BaseModel):
    """Persistent context for multi-turn policy experiments."""

    goal: str
    supervisor_policy_name: str
    action_policy_name: str
    raw_input: Optional[str] = Field(
        default=None,
        description="用户/CLI 原始输入(temporal 解析、router 改写之前);旧 log 无此字段则为 None",
    )
    router: Optional[dict] = Field(
        default=None,
        description="RouterResult {goal, needs_clarification, clarification};bin/runner 直跑路径未经 router，为 None",
    )
    turns: list[PolicyTurn] = Field(default_factory=list)
    task_type: Optional[TaskType] = None
    collection_scope: Optional[CollectionScope] = None
    content_notes: list[str] = Field(default_factory=list)
    content_note_hashes: list[str] = Field(default_factory=list)
    output: Optional[str] = None
    milestones: list[dict] = Field(
        default_factory=list,
        description="子目标分解结果 [{id, name, description, kind, success_condition}]",
    )
    models: dict[str, str] = Field(
        default_factory=dict,
        description="本次运行各 LLM 配置键实际使用的模型 {config_key: model}，用于成本核算自描述",
    )
