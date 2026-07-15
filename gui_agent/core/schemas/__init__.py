"""Shared schemas for policy experiments."""

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

if TYPE_CHECKING:
    from gui_agent.core.orchestrator.program import RunResult


# The 6 shared actions every platform supports. Platform-specific actions (iphone
# home/app_switch, browser navigate, android home/back/app_switch) live in each
# adapter's <Plat>Action.action_type, so a policy injects only its own vocabulary.
BaseActionType = Literal[
    "tap", "type", "clear_text", "press_enter", "scroll", "drag"
]
BaseScrollTargetArea = Literal[
    "main_content",
    "left_panel",
    "right_panel",
    "top_content",
    "bottom_content",
]
ScrollAmount = Literal["small", "medium", "large"]

# Neutral full-set labels for logging/HUD across all platforms (action_label is
# platform-agnostic; unknown types fall back to the raw string).
_ACTION_TYPE_LABELS: dict[str, str] = {
    "tap": "点击",
    "type": "输入",
    "clear_text": "清空",
    "press_enter": "回车",
    "scroll": "滚动",
    "drag": "拖动",
    "navigate": "导航",
    "home": "主屏",
    "back": "返回",
    "app_switch": "切换应用",
    "select_option": "选择选项",
}


def action_label(action_type: str) -> str:
    return _ACTION_TYPE_LABELS.get(action_type, action_type)
TaskType = Literal["action", "analysis"]
RunStatus = Literal["completed", "interrupted", "stopped"]
MilestoneStatus = Literal["pending", "running", "done", "failed"]
ChecklistStatus = Literal["pending", "done", "blocked", "skipped"]
MilestoneKind = Literal["navigation", "filter", "collection", "action", "verification"]
CompletionStrategy = Literal[
    "visible_once",
    "read_once",
    "scroll_until_boundary",
    "react_until_collected",
    "repeat_until_satisfied",
    "human_escalation",
]
AtomicRole = Literal["prepare", "write", "commit", "iterate"]
ActionFamily = Literal[
    "input", "select", "activate", "navigate", "iterate", "unknown"
]
ActionExecutionStatus = Literal["not_attempted", "dispatched", "dispatch_failed"]
ActionTargetStatus = Literal["on_target", "off_target", "unknown"]
ActionResponseStatus = Literal["observed", "none_observed", "unobservable", "unknown"]
EffectMode = Literal["ensure", "transform", "dispatch"]
PersistenceMode = Literal["immediate", "explicit_commit"]
EffectStatus = Literal["satisfied", "unmet", "contradicted", "unknown"]
EffectFreshness = Literal["preexisting", "current_run", "unknown"]
CompletionStatus = Literal["confirmed", "accepted_unverified", "failed", "in_progress"]
StatementPhase = Literal["completed", "failed", "exhausted", "infeasible", "interrupted"]
Verification = Literal["confirmed", "accepted_unverified"]
BindingSource = Literal["visual", "structural"]
BindingStatus = Literal["bound", "unresolved", "contradicted"]
TargetValue = str | list[str]


def target_value_options(value: TargetValue | object) -> tuple[str, ...]:
    """Return the ordered, non-empty values declared for one semantic field."""
    raw = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def normalize_effect_contract_fields(value: object) -> object:
    """Map retired mutation fields to the canonical effect/persistence contract."""
    if not isinstance(value, dict):
        return value
    data = dict(value)
    legacy_mode = str(data.get("mutation_mode") or "").strip().lower()
    legacy_boundary = bool(data.get("requires_commit") or data.get("require_fresh_action"))
    if not data.get("effect_mode"):
        if legacy_mode == "ensure":
            data["effect_mode"] = "ensure"
        elif legacy_mode == "change" and (data.get("target_values") or legacy_boundary):
            data["effect_mode"] = "transform"
        elif legacy_boundary:
            data["effect_mode"] = "dispatch"
    data.setdefault(
        "persistence",
        "explicit_commit" if data.get("requires_commit") else "immediate",
    )
    if data.get("kind") == "action" and not data.get("effect_mode"):
        data["effect_mode"] = (
            "transform"
            if data.get("target_values")
            else "dispatch"
            if data.get("persistence") == "explicit_commit" or data.get("returns")
            else None
        )
    return data

# 「连续操作」轴（与 kind 正交）：靠重复调整逼近目标的策略，区别于单步达成。
#   - repeat_until_satisfied：收敛到目标值（picker 调日期/时间、步进器、滑块）
#   - scroll_until_boundary：滚动采集直到边界（已有 loop 机制）
#   - react_until_collected：逐行/逐页集合遍历；系统维护 pending/current/completed rows，
#     planner 只执行下一步意图（打开行、返回列表、翻页/滚动）。
# 连续操作的「进展/卡住」判据与单步不同：进展=被监控值朝目标逼近/集合状态推进，重复同一操作属正常，
# 故 checker(进展传感器)/planner(收敛分流)/stuck(值停滞判据) 都按此轴分流。
ITERATIVE_STRATEGIES: tuple[str, ...] = (
    "repeat_until_satisfied",
    "scroll_until_boundary",
    "react_until_collected",
)


class TargetBinding(BaseModel):
    """One-shot result of binding a concrete write to a target identity."""

    status: BindingStatus
    source: Optional[BindingSource] = None
    unit_id: str = ""
    reason: str = ""


class MutationAuthorization(BaseModel):
    """One-shot permission to write one desired field on one resolved subject."""

    statement_id: str
    subject_ref: str
    field: str
    desired_value: str
    source: BindingSource


class MutationReceipt(BaseModel):
    """Immutable proof that one authorized mutation write crossed the UI boundary.

    Post-action state is deliberately absent: effects belong to observations, while this
    receipt records dispatch provenance only.
    """

    statement_id: str
    subject_ref: str
    field: str
    intended_value: str
    source: BindingSource


class ActionSignal(BaseModel):
    """Structured lifecycle of one atomic UI action.

    Execution answers whether the event crossed the GUI boundary; response answers whether the
    page visibly reacted. Business outcome and persistence are assessed separately and must not
    be inferred from these delivery facts.
    """

    action_key: str = ""
    role: AtomicRole = "prepare"
    surface_id: str = Field(
        default="",
        description="动作派发时所在的活动交互表面身份；由平台 adapter 产生，视觉平台可为空。",
    )
    target_control: str = ""
    target_value: str = ""
    mutation_receipt: Optional[MutationReceipt] = None
    binding: Optional[TargetBinding] = None
    execution: ActionExecutionStatus = "not_attempted"
    target: ActionTargetStatus = "unknown"
    response: ActionResponseStatus = "unknown"
    response_channels: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    suppressed_reason: str = ""


class EffectSignal(BaseModel):
    """Persisted statement-level business-effect evidence.

    It is deliberately separate from ``ActionSignal``: a correctly dispatched action may still
    have an unknown or contradicted business result.
    """

    statement_id: str = ""
    status: EffectStatus = "unknown"
    freshness: EffectFreshness = "unknown"
    subject_ref: str = ""
    source_type: str = ""
    authoritative: bool = False
    evidence: list[str] = Field(default_factory=list)


class CollectionScope(BaseModel):
    """Structured scope for collected content."""

    label: str = Field(default="", description="范围标签，如目标范围、当前分组、自定义条件")
    start: Optional[str] = Field(default=None, description="范围开始值；不可确定则为空")
    end: Optional[str] = Field(default=None, description="范围结束值；不可确定则为空")
    evidence: list[str] = Field(default_factory=list, description="截图中支持该范围的可见证据")


class RunState(BaseModel):
    """Run-level terminal state persisted with the context."""

    status: Optional[RunStatus] = Field(
        default=None,
        description="本次运行的最终状态：completed=执行到终态，interrupted=用户中止，stopped=中途停止",
    )
    stop_reason: str = Field(default="", description="本次运行的最终停止原因")
    execution_completed: bool = Field(
        default=False,
        description="程序是否已执行到终态；可为 true 而业务效果仍未确认",
    )
    goal_completed: bool = Field(default=False, description="本次运行是否确认完成用户目标")
    goal_status: Literal["confirmed", "accepted_unverified", "incomplete"] = Field(
        default="incomplete",
        description="目标效果确认级别：confirmed、accepted_unverified 或 incomplete",
    )
    output: Optional[str] = Field(default=None, description="最终输出")


class MilestoneChecklistItem(BaseModel):
    """One persisted milestone-local progress/check item."""

    id: str = Field(description="checklist 项稳定 ID")
    text: str = Field(description="需要确认的条件或子项")
    status: ChecklistStatus = Field(default="pending", description="pending | done | blocked | skipped")
    evidence: list[str] = Field(default_factory=list, description="支持该状态的可见证据")
    source: str = Field(default="", description="产生/更新该项的来源，如 checker/planner/manual")


def split_acceptance_items(success_condition: str, fallback: str = "") -> list[str]:
    """Split a milestone success_condition into individual acceptance items.

    Shared by the checker (to enumerate items for per-item judgement) and the state derivation
    (to map per-item verdicts back) so the two agree on item identity and ordering. Splits on
    newlines and ; / ；, trims bullet markers, caps at 8 items, never returns empty."""
    source = (success_condition or fallback or "完成当前子目标").strip()
    parts = [p.strip(" \t\r\n-•*") for p in re.split(r"[\n;；]+", source)]
    parts = [p for p in parts if p]
    return parts[:8] or [source]


class MilestoneState(BaseModel):
    """Runtime state for one milestone, separated from the static decomposition."""

    id: str
    status: Optional[MilestoneStatus] = Field(
        default=None,
        description="该 milestone 当前执行状态",
    )
    retry_count: int = Field(default=0, description="该 milestone 已重试次数")
    done_check: dict = Field(default_factory=dict, description="最终验收 checker 结果")
    checklist: list[MilestoneChecklistItem] = Field(
        default_factory=list,
        description="milestone-local checklist；由 checker/人工状态更新，不作为通用 LLM 待办清单",
    )
    reads: dict[str, str] = Field(
        default_factory=dict,
        description="编排/读取阶段提取出的结构化字段",
    )
    note_hashes: list[str] = Field(default_factory=list, description="该 milestone 采集入库的内容片段哈希")
    last_summary: str = Field(default="", description="最近一次 supervisor summary")
    last_turn_index: Optional[int] = Field(default=None, description="最近一次关联 turn 序号")
    last_page_identity: str = Field(default="", description="checker 最近识别的页面身份")
    scroll_count: int = Field(default=0, description="该 milestone 已尝试滚动次数")
    progress_values: list[str] = Field(default_factory=list, description="连续调值类最近观测到的值")
    pre_existing: bool = Field(default=False, description="是否为会话前已存在状态")
    collection_summary: Optional[str] = Field(default=None, description="采集完成摘要")
    collection_scope: Optional[CollectionScope] = Field(default=None, description="最近一次采集范围")


class BaseAction(BaseModel):
    """The platform-neutral action: the 7 shared actions + fields every platform's
    scroll/drag executor consumes. Each adapter subclasses this (adapters/<plat>/
    actions.py) to add its own action_type values + platform-specific fields, so a
    policy injects ONLY its platform's vocabulary into the LLM (no cross-platform leak).
    """

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
                if action_type == "select_option" and text:
                    data["description"] = f"选择下拉选项 {text}"
                elif text:
                    data["description"] = f"执行{action_type}并输入{text}"
                else:
                    data["description"] = f"执行{action_type}操作"
            # NOTE: the iPhone status-bar / home-indicator dead-zone clamp that
            # used to live here moved into the iphone executor (S3) — it is a
            # device-screen concern and must NOT apply to other platforms (it was
            # mis-clicking the top/bottom of web pages on the browser adapter).
        return data

    action_type: BaseActionType = Field(
        description="操作类型：tap、type、press_enter、clear_text、scroll、drag 之一"
    )
    x: Optional[float] = Field(
        default=None,
        description="归一化 x 坐标（0-1000）。tap/type 为目标中心；scroll/drag 为滚动锚点；不需要坐标的动作可留空",
    )
    y: Optional[float] = Field(
        default=None,
        description="归一化 y 坐标（0-1000）。tap/type 为目标中心；scroll/drag 为滚动锚点；不需要坐标的动作可留空",
    )
    direction: Optional[str] = Field(
        default=None,
        description="内容方向：up（查看上方内容）、down（查看下方内容）、left（查看右侧内容）、right（查看左侧内容）。普通列表 scroll/drag 使用",
    )
    target_area: BaseScrollTargetArea = Field(
        default="main_content",
        description="滚动目标区域：main_content/left_panel/right_panel/top_content/bottom_content",
    )
    amount: ScrollAmount = Field(
        default="medium",
        description="滚动幅度：small/medium/large。普通翻看用 medium，细微调整用 small，快速翻页用 large",
    )
    to_x: Optional[float] = Field(
        default=None,
        description="drag 的可选结束点归一化 x 坐标；不需要指定结束点时留空",
    )
    to_y: Optional[float] = Field(
        default=None,
        description="drag 的可选结束点归一化 y 坐标；不需要指定结束点时留空",
    )
    duration_ms: Optional[int] = Field(
        default=None,
        description="drag 的可选持续时间毫秒；通常留空",
    )
    text: Optional[str] = Field(
        default=None,
        description="要输入的文字内容（action_type 为 type 时必填）",
    )
    description: str = Field(description="该操作的中文说明，如「点击搜索按钮」")
    snap: Optional[dict] = Field(
        default=None,
        description="可选定位辅助信息；通常留空",
    )

    @model_validator(mode="after")
    def _require_text_for_type(self) -> "BaseAction":
        if self.action_type == "type" and not self.text:
            # The model frequently emits `type` with empty text to mean "clear the field"
            # (description like 「清空输入框」). `clear_text` is the proper action for that — coerce
            # to it instead of raising, which otherwise crashes the whole run on one malformed
            # action decision (observed as: type with empty text for "clear a filter input").
            self.action_type = "clear_text"
        # value_direction is an iphone-only field (picker); getattr keeps this base
        # validator correct for both the base and the iphone subclass.
        if self.action_type in {"scroll", "drag"} and not (
            self.direction or getattr(self, "value_direction", None)
        ):
            raise ValueError("scroll/drag 动作必须填写 direction 或 value_direction")
        return self


class Observation(BaseModel):
    """Raw environment observation used by policies."""

    png_bytes: bytes = Field(description="当前 iPhone 截图 PNG bytes")
    source: str = Field(description="观测来源")
    loading: Optional[bool] = Field(
        default=None,
        description=(
            "平台感知层的「页面是否仍在加载」结构信号（如 web 的 document.readyState!=complete）。"
            "None=该平台不提供此信号，由 supervisor 退回视觉白屏启发式判断。"
        ),
    )
    url: Optional[str] = Field(
        default=None,
        description=(
            "平台感知层提供的当前页面 URL（如浏览器地址）。结构化元信息——它**不在截图里**"
            "（vision-only 截图只含网页 viewport），可作为页面身份/验收的辅助信号，"
            "免得它从看不见的地址栏编造。None=该平台不提供（iphone/android 留空）。"
        ),
    )
    title: Optional[str] = Field(
        default=None,
        description="平台感知层提供的当前页面/标签标题（如浏览器 document.title）。同样不在截图里。None=不提供。",
    )
    dom_state: Optional[str] = Field(
        default=None,
        description=(
            "平台感知层提供的页面交互状态指纹（如浏览器表单控件值/选中状态的哈希）。"
            "逐字段填表时像素几乎不变、指令文本高度相似，但该指纹每轮都变——"
            "作为确定性进展信号抑制 stuck/重复误判（与 url 同模式）。None=该平台不提供。"
        ),
    )
    tables: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "平台感知层提供的当前页面表格/网格结构快照。"
            "用于表格类 read 任务优先按行列读取，避免靠视觉滚动/OCR 对齐。None=该平台不提供或当前页无表格。"
        ),
    )
    form_controls: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "平台感知层提供的当前可见表单控件结构摘要（如浏览器 input/select/textarea 的 label、"
            "类型、当前值和可选项）。用于让规划器区分 native select 等截图不可见弹层；"
            "不包含表格行数据。None=该平台不提供或当前页无表单控件。"
        ),
    )
    form_controls_meta: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "form_controls 结构清单的覆盖率元数据：total_rendered/returned/truncated/coverage/"
            "raw_limit_hit。用于区分『清单中没有』与『清单截断后未返回』；不提供该传感器的平台留空。"
        ),
    )
    viewport: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "页面级可滚动/可翻页区域（view window）的遍历边界信号："
            "{type: 'paged'|'scroll'|'unknown', page_index, page_count, has_next_page, "
            "has_prev_page, can_scroll_more, at_scroll_end}。仅驱动页面级/视觉集合；表格采集必须"
            "使用产生该表格行的 tables[i].traversal，避免消费其它滚动区或分页器的信号。"
            "None=该平台/当前帧未探测到页面级遍历信号（遍历退回像素冻结兜底）。"
        ),
    )
    semantic_tree: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "平台感知层提供的页面语义树（浏览器 v2 路径）。"
            "每个节点：{role, key, value, ref, depth}，其中 role=ARIA 角色、"
            "key=可访问名称（标签文本）、value=当前值（表单控件）、"
            "ref=backendDOMNodeId（用于 DOM 直达动作：点击/滚动/读值，与折叠无关）、"
            "depth=嵌套深度。是 read_selector / click_selector / resolve_target 的数据来源；"
            "None=该平台不提供（iphone/android）或当前页探测失败。"
        ),
    )
    applied_filters: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "平台感知层提供的「当前已生效筛选」结构状态（{列名/维度: 值}，例 "
            "{'<字段A>': '<范围值>', '<字段B>': '<枚举值>'}）。"
            "这是筛选控件自身的权威状态——筛选「是否已生效」的确定性信号，与表格里展示了哪些行/列"
            "无关。filter 类里程碑据此判「动作已生效」(action-applied)，与「行内容是否符合期望」"
            "(effect) 解耦，避免 checker 拿表格展示列(某个由被筛字段派生/相邻的展示列)推翻一个已正确生效的筛选。"
            "各 adapter 负责把本平台/页面的筛选状态表示（状态指示器、地址/状态编码、筛选控件状态等）"
            "翻译成这个平台中性契约。None=该平台不提供、当前页无已生效筛选，或当前页没有可判定的筛选状态。"
        ),
    )
    applied_filter_meta: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "平台感知层提供的已生效筛选取数元信息。字段由 adapter 定义，但应表达取数来源、"
            "状态指示通道是否存在、是否存在可替代的筛选状态通道等。"
            "用于区分「证据通道不存在」和「证据通道存在但为空」，避免 checker 把缺少某种 UI 形态"
            "误读成任务未完成。"
        ),
    )


class RecoveryNotice(BaseModel):
    """A recovery event emitted while executing one statement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cls: str
    mechanism: str
    site: str
    detail: str = ""
    outcome: str = ""


class StatementOutcome(BaseModel):
    """The authoritative terminal result of exactly one Program statement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: StatementPhase
    summary: str
    verification: Optional[Verification] = None
    kickback: Optional[str] = None
    reads: dict[str, str] = Field(default_factory=dict)
    rows: list[dict[str, str]] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    observation: Observation | None = None
    observation_url: str | None = None
    executed_sql: str = ""
    context_reports: list[dict] = Field(default_factory=list)
    recovery_notices: list[RecoveryNotice] = Field(default_factory=list)
    failure_evidence: str | None = None

    @model_validator(mode="after")
    def _validate_terminal_phase(self) -> "StatementOutcome":
        if self.phase == "completed":
            if self.verification not in ("confirmed", "accepted_unverified"):
                raise ValueError(
                    "completed StatementOutcome requires confirmed|accepted_unverified"
                )
            if self.kickback:
                raise ValueError("completed StatementOutcome cannot carry kickback")
        else:
            if self.verification is not None:
                raise ValueError(f"{self.phase} StatementOutcome cannot carry verification")
            if self.phase == "infeasible":
                if not (self.kickback and self.kickback.strip()):
                    raise ValueError("infeasible StatementOutcome requires kickback")
            elif self.kickback:
                raise ValueError(
                    f"{self.phase} StatementOutcome cannot carry kickback"
                )
        return self

    @classmethod
    def completed(
        cls,
        summary: str,
        *,
        verification: Verification = "confirmed",
        **details: Any,
    ) -> "StatementOutcome":
        return cls(
            phase="completed",
            summary=summary,
            verification=verification,
            **details,
        )

    @classmethod
    def _failure(
        cls,
        phase: Literal["failed", "exhausted", "interrupted"],
        summary: str,
        details: dict[str, Any],
    ) -> "StatementOutcome":
        details.setdefault("failure_evidence", summary)
        return cls(phase=phase, summary=summary, **details)

    @classmethod
    def failed(cls, summary: str, **details: Any) -> "StatementOutcome":
        return cls._failure("failed", summary, details)

    @classmethod
    def exhausted(cls, summary: str, **details: Any) -> "StatementOutcome":
        return cls._failure("exhausted", summary, details)

    @classmethod
    def interrupted(cls, summary: str, **details: Any) -> "StatementOutcome":
        return cls._failure("interrupted", summary, details)

    @classmethod
    def infeasible(
        cls,
        summary: str,
        *,
        kickback: str,
        **details: Any,
    ) -> "StatementOutcome":
        details.setdefault("failure_evidence", summary)
        return cls(
            phase="infeasible",
            summary=summary,
            kickback=kickback,
            **details,
        )

    @property
    def is_completed(self) -> bool:
        return self.phase == "completed"

    def to_run_result(self) -> "RunResult":
        """Project into the Interpreter's internal wire type."""
        from gui_agent.core.orchestrator.program import RunResult

        return RunResult(
            completed=self.is_completed,
            failed=not self.is_completed,
            completion_status=self.verification if self.is_completed else "failed",
            reads=dict(self.reads),
            rows=list(self.rows),
            summary=self.summary,
            evidence=list(self.evidence),
        )


class BaseActionDecision(BaseModel):
    """Action policy output: one physical action or an explicit grounding failure."""

    # SerializeAsAny so model_dump_json preserves per-platform Action subclass fields
    # (e.g. iphone value_direction, browser url). Without it, a base-typed field drops
    # subclass-only fields on serialization (context.json in runner.py:285).
    action: Optional[SerializeAsAny[BaseAction]] = Field(
        description="当前应该执行的物理操作；无法定位可执行目标时为 null",
    )
    not_found_reason: Optional[str] = Field(
        default=None,
        description=(
            "action=null 时说明为什么当前帧无法定位可执行目标；它不是任务完成或终止信号，"
            "找到目标时留空"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_flat_action(cls, data: object) -> object:
        """Repair two common model malformations of the action wrapper.

        1. Flat: action fields at the top level, no ``action`` wrapper → wrap them.
        2. ``action`` given as a bare STRING (the model emitted only the type, e.g.
           ``{"action": "tap", "x": 67, "y": 175, ...}``) → rebuild the nested object with
           ``action_type`` = that string and the sibling fields. Without this the json_object
           primary parse fails and we fall back to a second (text-JSON) LLM call.
        """
        if not isinstance(data, dict):
            return data
        raw_action = data.get("action")
        action_type = (
            raw_action.get("action_type")
            if isinstance(raw_action, dict)
            else raw_action
        )
        if action_type == "stop" or data.get("action_type") == "stop":
            description = (
                raw_action.get("description")
                if isinstance(raw_action, dict)
                else data.get("description")
            )
            return {
                "action": None,
                "not_found_reason": data.get("not_found_reason")
                or description
                or "legacy action policy returned stop",
            }
        if data.get("not_found_reason"):
            return {**data, "action": None}
        if "action_type" in data and "action" not in data:
            return {"action": data}
        if isinstance(data.get("action"), str):
            nested = {k: v for k, v in data.items() if k not in ("action", "not_found_reason")}
            nested["action_type"] = data["action"]
            out: dict = {"action": nested}
            if data.get("not_found_reason") is not None:
                out["not_found_reason"] = data["not_found_reason"]
            return out
        return data

    @model_validator(mode="after")
    def _require_action_or_reason(self) -> "BaseActionDecision":
        if self.action is None and not str(self.not_found_reason or "").strip():
            raise ValueError("action=null 时必须填写 not_found_reason")
        return self


class SupervisorStep(BaseModel):
    """Supervisor policy decision for one turn."""

    model_config = ConfigDict(extra="forbid")

    should_act: bool = Field(description="是否调用 action policy 执行动作")
    instruction: Optional[str] = Field(
        default=None,
        description="给 action policy 的精确操作指令（should_act=true 时必填）",
    )
    outcome: Optional[StatementOutcome] = Field(
        default=None,
        description="当前 statement 的权威终态；None 表示仍在执行。",
    )
    app_name: Optional[str] = Field(default=None, description="当前前台应用名称")
    summary: str = Field(description="对当前屏幕状态和任务进展的简要描述")
    preformed_action: Optional[BaseActionDecision] = Field(
        default=None,
        description="预生成的动作决策（设置后 runner 跳过 Action Policy 直接执行）",
    )
    read_instruction: Optional[str] = Field(
        default=None,
        description="当前屏幕需要提取的内容说明（analysis 任务时由 Checker 填写）",
    )
    allow_read: bool = Field(default=False, description="是否允许 runner 将读取结果写入 content_notes")
    milestone_id: Optional[str] = Field(default=None, description="当前子目标 ID")
    execution_scope: str = Field(
        default="",
        description=(
            "当前执行上下文分桶 key；stuck/no-effect/history 等运行时记忆按此隔离。"
            "普通任务通常为 milestone:<id>，逐行/逐实体任务可为 row:<identity>。"
        ),
    )
    milestone_kind: Optional[MilestoneKind] = Field(default=None, description="当前子目标类型")
    completion_strategy: Optional[CompletionStrategy] = Field(default=None, description="当前子目标完成策略")
    atomic_role: AtomicRole = Field(
        default="prepare",
        description="当前原子动作在交互 Run 中的角色：prepare=准备状态，commit=最终副作用派发，iterate=允许有进展地重复。",
    )
    action_family: ActionFamily = Field(
        default="unknown",
        description=(
            "planner 指令要求的原子动作族。runner 在派发前校验 concrete primitive；"
            "unknown 表示当前无法确定具体 UI 原语；提交语义只由 atomic_role=commit 表达。"
        ),
    )
    target_control: str = Field(
        default="",
        description="planner 声明的本轮控件/字段目标；执行前与 milestone target_controls 对齐。",
    )
    target_value: str = Field(
        default="",
        description="本轮写入/选择的结构化目标值；为空时由平台策略按原有路径决策。",
    )
    mutation_authorization: Optional[MutationAuthorization] = Field(
        default=None,
        description="执行层生成的一次性 mutation 写授权；不属于 planner/DSL 输出。",
    )
    requires_mutation_authorization: bool = Field(
        default=False,
        description="当前 write 是否必须持有系统生成的 mutation authorization。",
    )
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
    # 由 checker 的 page_identity 和平台配置的 home markers 派生，非 LLM 填写。
    is_home_screen: bool = Field(
        default=False,
        description="当前是否为该平台的系统主屏/启动器界面",
    )
    # 页面未稳定（白屏/加载中）的等待帧：runner 据此跳过本帧、不计入 max_turns、不累加 noop。
    is_loading: bool = Field(
        default=False,
        description="当前帧页面尚未渲染稳定（白屏/加载中），应等待重新观察而非执行/计数",
    )

    @model_validator(mode="after")
    def _separate_running_and_terminal_decisions(self) -> "SupervisorStep":
        if self.outcome is not None and (self.should_act or self.is_loading):
            raise ValueError(
                "terminal SupervisorStep cannot request an action or loading wait"
            )
        return self


class GoalValidationResult(BaseModel):
    """Result of independent goal-completion validation."""

    sufficient: bool = Field(description="已收集数据是否充分回答了用户目标")
    missing: str = Field(default="", description="缺少什么（sufficient=false 时填写）")


class StatementContract(BaseModel):
    """Frozen execution contract for one Program statement invocation input.

    Runtime status lives on StatementRuntimeState / StatementOutcome, never here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

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
            normalized = normalize_effect_contract_fields(normalized)
            # Keep only declared contract fields (drop runtime/legacy extras).
            allowed = {
                "id", "name", "description", "success_condition", "kind",
                "completion_strategy", "precondition", "effect_mode", "persistence",
                "target_controls", "target_values", "returns", "read_spec",
                "scroll_stop_condition", "observable_boundary", "scroll_budget",
                "failure_hints",
            }
            return {k: v for k, v in normalized.items() if k in allowed}
        return data

    id: str
    name: str
    description: str
    success_condition: str
    kind: MilestoneKind = Field(
        default="action",
        description="navigation | filter | collection | action | verification",
    )
    completion_strategy: CompletionStrategy = Field(
        default="visible_once",
        description=(
            "visible_once | read_once | scroll_until_boundary | "
            "react_until_collected | repeat_until_satisfied | human_escalation"
        ),
    )
    precondition: bool = Field(
        default=False,
        description="True when this milestone ensures an entry state and may already be satisfied on the first frame.",
    )
    effect_mode: Optional[EffectMode] = Field(
        default=None,
        description="业务效果语义；None 表示普通交互状态转换，不声明持久化业务效果。",
    )
    persistence: PersistenceMode = Field(
        default="immediate",
        description="业务效果是立即生效，还是需要独立持久化提交边界。",
    )
    target_controls: list[str] = Field(
        default_factory=list,
        description="该执行单元必须命中的字段、控件或集合能力名称。",
    )
    target_values: dict[str, TargetValue] = Field(
        default_factory=dict,
        description=(
            "该执行单元要求实现的结构化业务终态；action 可用数组表示同一选择组必须同时"
            "满足的精确值集合，重复集合成员仍使用各自的标量合同。它不提供目标身份或写入授权。"
        ),
    )
    returns: list[str] = Field(
        default_factory=list,
        description="声明的结构化返回字段；由编排器 Run.returns 填充，空 = 本 milestone 无出参。"
                    "Milestone 返回后由 statement result contract 校验。",
    )
    read_spec: str = Field(
        default="",
        description="返回字段的判读说明（对应 Run.read_spec）；与 returns 一起构成出参合同的结构化通道。",
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


class StatementInfo(BaseModel):
    """Persisted statement contract DTO written once per invocation (first turn)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    name: str = ""
    description: str = ""
    kind: str = ""
    success_condition: str = ""
    completion_strategy: str = ""
    precondition: bool = False
    effect_mode: Optional[EffectMode] = None
    persistence: PersistenceMode = "immediate"
    target_controls: list[str] = Field(default_factory=list)
    target_values: dict[str, TargetValue] = Field(default_factory=dict)
    returns: list[str] = Field(default_factory=list)
    read_spec: str = ""
    sql: str = ""
    data_scope: str = ""


# Back-compat alias: package paths and many imports still say Milestone.
Milestone = StatementContract


class TargetVerify(BaseModel):
    """Post-action targeting verify: did the snapped tap land on the intended element."""

    on_target: bool = Field(description="标记圆环是否正好落在指令意图的目标元素上")
    actual_element: str = Field(default="", description="标记实际落在的元素（如「转账 tab」「搜索框」）")
    reason: str = Field(default="", description="一句话理由")


class PolicyTurn(BaseModel):
    """One observe-decide-act turn saved in continue mode."""

    index: int
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    operation_mode: Literal["interactive", "observation", "non_interactive"] = Field(
        default="interactive",
        description=(
            "本轮是 UI 交互执行、无动作观察仲裁，还是非 UI primitive"
            "（如 structured read / data_query）"
        ),
    )
    observation_source: str
    observation_url: str = Field(default="", description="本轮观察帧对应的截图文件名；为空时报告层按 turn index 回退推断")
    statement: Optional[StatementInfo] = Field(
        default=None,
        description="本 statement invocation 首条 turn 的合同快照；后续 turn 为 None",
    )
    statement_instance_id: str = Field(
        default="",
        description="一次 statement 调用的实例 id（foreach 同 statement 多次调用互不相同）",
    )
    supervisor: SupervisorStep
    action_decision: Optional[BaseActionDecision] = None
    non_ui: Optional[dict[str, Any]] = Field(
        default=None,
        description="非 UI primitive 执行明细：kind/sql/returns/reads/completed 等；interactive turn 留空",
    )
    checker: Optional[dict] = Field(default=None, description="Checker 原始结果：status, reason, summary, missing_evidence 等")
    planner: Optional[dict] = Field(default=None, description="Planner 原始结果：instruction, summary, direction, drag_column")
    replan: Optional[dict] = Field(default=None, description="Replan 原始结果：diagnosis, strategy, instruction")
    executed: bool = False
    action_signal: Optional[ActionSignal] = Field(
        default=None,
        description="动作派发、目标命中与页面响应的结构化信号；旧日志可为空。",
    )
    effect_signal: Optional[EffectSignal] = Field(
        default=None,
        description="statement 级业务效果证据；与原子动作信号分离。",
    )
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
    sections_loaded: list[str] = Field(
        default_factory=list,
        description="本轮 planner 实际注入的渐进知识章节名（KnowledgeSelector 按 (milestone, page) 选定并缓存）；无渐进知识或未选中则为空",
    )
    llm_context: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "本轮 LLM 上下文诊断：实际 prompt 快照、context budget included/dropped blocks、"
            "估算 chars/tokens、block source/priority/ttl/裁剪原因，以及 KnowledgeSelector "
            "cache/fallback/section_ids。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_action_outcome(cls, data: object) -> object:
        """Move historical per-action outcome fields into statement-level effect evidence."""
        if not isinstance(data, dict) or data.get("effect_signal") is not None:
            return data
        signal = data.get("action_signal")
        if not isinstance(signal, dict):
            return data
        outcome = str(signal.get("outcome") or "unverified")
        if outcome not in {"confirmed", "contradicted"}:
            return data
        supervisor = data.get("supervisor") if isinstance(data.get("supervisor"), dict) else {}
        migrated = dict(data)
        migrated["effect_signal"] = {
            "statement_id": str(supervisor.get("milestone_id") or ""),
            "status": "satisfied" if outcome == "confirmed" else "contradicted",
            "freshness": "current_run",
            "source_type": "legacy.action_outcome",
            "evidence": list(signal.get("outcome_evidence") or ()),
        }
        return migrated

    @model_validator(mode="after")
    def _normalize_no_action_signal(self) -> "PolicyTurn":
        """A turn without a physical action cannot contain dispatch evidence.

        This also repairs historical contexts where the action policy returned the
        now-retired ``stop`` primitive and the executor recorded that no-op as a
        successful dispatch.
        """
        if self.action_decision is None or self.action_decision.action is not None:
            return self
        self.executed = False
        if self.action_signal is not None:
            self.action_signal.action_key = ""
            self.action_signal.execution = "not_attempted"
            self.action_signal.mutation_receipt = None
            self.action_signal.response = "unknown"
            self.action_signal.response_channels.clear()
            self.effect_signal = None
        return self


class PolicyContext(BaseModel):
    """Persistent context for multi-turn policy experiments."""

    goal: str
    supervisor_policy_name: str
    action_policy_name: str
    platform: Optional[str] = Field(
        default=None,
        description="运行平台 iphone/browser/android(AGENT_PLATFORM);旧 log 无此字段则为 None",
    )
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
    run: RunState = Field(default_factory=RunState, description="本次运行的结构化状态")
    models: dict[str, str] = Field(
        default_factory=dict,
        description="本次运行各 LLM 配置键实际使用的模型 {config_key: model}，用于成本核算自描述",
    )
    knowledge: Optional[dict] = Field(
        default=None,
        description="本次注入的应用知识摘要 {app_name, nav_chars, elements_chars, section_count}；未命中知识库则为 None。每轮实际注入的章节见 turns[].sections_loaded",
    )
    wall_clock_s: Optional[float] = Field(
        default=None,
        description="本次 run_agent_loop 端到端真实墙钟耗时(秒)；含 LLM、settle、感知/执行/调度等全部。旧 log 无此字段则为 None",
    )
    orchestrator: Optional[dict] = Field(
        default=None,
        description="DSL Program 运行信息：{program: {goal, statements:[run/if/finish]}}。"
                    "decompose 是独立阶段，报告据此渲染单独的「分解」行。",
    )



# --- Back-compat aliases -----------------------------------------------------
# Many modules ``from gui_agent.core.schemas import Action, ActionDecision``. The
# neutral classes were renamed to BaseAction/BaseActionDecision (so adapters can
# subclass per platform); these aliases keep every existing importer working. Files
# that read platform-specific fields (iphone picker, browser url) import their
# adapter's <Plat>Action instead — the runtime object is always the right subclass.
Action = BaseAction
ActionDecision = BaseActionDecision
