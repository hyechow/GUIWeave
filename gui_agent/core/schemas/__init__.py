"""Shared schemas for policy experiments."""

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializeAsAny,
    field_validator,
    model_validator,
)

from gui_agent.core.filter_contract import (
    AppliedFilterState,
    FilterPredicateSet,
)

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
    "scroll_to_ref": "定位滚动",
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
ProgramPhase = Literal["completed", "failed", "interrupted", "stopped"]
AtomicRole = Literal["prepare", "write", "commit", "iterate"]
ActionFamily = Literal[
    "input", "select", "activate", "navigate", "iterate", "unknown"
]
ActionExecutionStatus = Literal["not_attempted", "dispatched", "dispatch_failed"]
ActionTargetStatus = Literal["on_target", "off_target", "unknown"]
ActionResponseStatus = Literal["observed", "none_observed", "unobservable", "unknown"]
PersistenceMode = Literal["immediate", "explicit_commit"]
OutputType = Literal["text", "number", "boolean", "url", "record", "list[record]", "json"]
Coverage = Literal["current_view", "complete", "best_effort"]
CompletionStatus = Literal["confirmed", "accepted_unverified", "failed", "in_progress"]
StatementPhase = Literal["completed", "failed", "exhausted", "infeasible", "interrupted"]
Verification = Literal["confirmed", "accepted_unverified"]
BindingSource = Literal["visual", "structural"]
BindingStatus = Literal["bound", "contradicted", "unresolved"]
ActionEffectKind = Literal[
    "query_control",
    "presentation",
    "viewport",
    "pagination",
    "navigation",
    "field_write",
    "business_commit",
    "authentication",
    "unknown",
]
TargetValue = str | list[str]


class OutputSpec(BaseModel):
    """Shared typed output contract used by Program and executor runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: OutputType = "text"
    required: bool = True
    description: str = ""
    coverage: Coverage = "current_view"
    fields: tuple[str, ...] = Field(
        default_factory=tuple,
        description="required record keys for typed record/list[record] outputs",
    )

    @field_validator("fields", mode="before")
    @classmethod
    def _normalize_fields(cls, value):
        fields = tuple(str(item).strip() for item in (value or ()))
        if any(not item for item in fields):
            raise ValueError("output fields cannot contain empty names")
        if len(fields) != len(set(fields)):
            raise ValueError("output fields must be unique")
        return fields

    @model_validator(mode="after")
    def _record_fields_only(self) -> "OutputSpec":
        if self.fields and self.type not in {"record", "list[record]"}:
            raise ValueError("output fields require record or list[record] type")
        return self


def target_value_options(value: TargetValue | object) -> tuple[str, ...]:
    """Return the ordered, non-empty values declared for one semantic field."""
    raw = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


class TargetBinding(BaseModel):
    """One-shot result of binding a concrete action to a target identity."""

    status: BindingStatus
    source: Optional[BindingSource] = None
    unit_id: str = ""
    effect_kind: ActionEffectKind = "unknown"
    reason: str = ""


class MutationReceipt(BaseModel):
    """Immutable proof that one bound mutation write crossed the UI boundary.

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


class ActionIntent(BaseModel):
    """Immutable one-frame semantic action message.

    It is created by one Statement decision and consumed by the platform adapter. It has no
    lifecycle, retry counter, or independent persistence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instruction: str = Field(min_length=1)
    role: AtomicRole = "prepare"
    family: ActionFamily = "unknown"
    target_control: str = ""
    target_value: str = ""
    target_ref: str = Field(
        default="",
        description=(
            "当前 Observation 暴露的一次性结构引用；仅用于把本帧语义目标贯通到"
            " grounding/dispatch，不是跨帧状态"
        ),
    )
    expected_result: str = Field(
        default="",
        description=(
            "动作后下一帧应出现的可观察变化；只帮助 Action Policy 消歧目标，"
            "不授予完成判定权"
        ),
    )
    direction: Optional[
        Literal["up", "down", "left", "right", "increase", "decrease"]
    ] = None
    drag_column: Optional[str] = None
    drag_steps: Optional[int] = None


class CollectionScope(BaseModel):
    """Structured scope for collected content."""

    label: str = Field(default="", description="范围标签，如目标范围、当前分组、自定义条件")
    start: Optional[str] = Field(default=None, description="范围开始值；不可确定则为空")
    end: Optional[str] = Field(default=None, description="范围结束值；不可确定则为空")
    evidence: list[str] = Field(default_factory=list, description="截图中支持该范围的可见证据")


class ProgramOutcome(BaseModel):
    """Immutable terminal projection of the whole Program."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: ProgramPhase
    summary: str
    verification: Optional[Verification] = None
    output: Optional[str] = None

    @model_validator(mode="after")
    def _validate_verification(self) -> "ProgramOutcome":
        if self.phase == "completed" and self.verification is None:
            raise ValueError("completed ProgramOutcome requires verification")
        if self.phase != "completed" and self.verification is not None:
            raise ValueError(f"{self.phase} ProgramOutcome cannot carry verification")
        return self

def split_acceptance_items(success: str, fallback: str = "") -> list[str]:
    """Split a statement success contract into individual acceptance items.

    Shared by the checker (to enumerate items for per-item judgement) and the state derivation
    (to map per-item verdicts back) so the two agree on item identity and ordering. Splits on
    newlines and ; / ；, trims bullet markers, caps at 8 items, never returns empty."""
    source = (success or fallback or "完成当前子目标").strip()
    parts = [p.strip(" \t\r\n-•*") for p in re.split(r"[\n;；]+", source)]
    parts = [p for p in parts if p]
    return parts[:8] or [source]


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

    # Open on the base on purpose. Each adapter subclasses BaseAction to add its own
    # action_type values (browser: select_option / navigate / select_tab / upload …) and a
    # persisted PolicyTurn is reloaded THROUGH this base type. A closed Literal here rejects
    # those extensions on load (SerializeAsAny helps dump, not validate), which broke
    # checkpoint resume and replay for any browser run containing such actions. The subclass
    # re-constrains action_type to its platform Literal; the base only round-trips it.
    action_type: str = Field(
        description="操作类型：tap、type、press_enter、clear_text、scroll、drag 之一（平台子类扩展更多）"
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
    form_control_state: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "平台感知层在截断决策清单前保留的完整表单状态索引。"
            "仅供 Runtime 做声明目标值验收，不直接注入 LLM 上下文。"
        ),
    )
    form_control_state_meta: Optional[dict[str, Any]] = Field(
        default=None,
        description="完整表单状态索引的覆盖率元数据；不提供该传感器的平台留空。",
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
    applied_filter_state: Optional[AppliedFilterState] = Field(
        default=None,
        description=(
            "平台 adapter 规范化后的类型化筛选谓词和覆盖率。constrain 的确定性终态只使用"
            "该字段；applied_filters 仅保留为显示/旧日志兼容。"
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
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    observation: Observation | None = None
    observation_url: str | None = None
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

class BaseActionDecision(BaseModel):
    """Action policy output: exactly one physical action."""

    model_config = ConfigDict(extra="forbid")

    # SerializeAsAny so model_dump_json preserves per-platform Action subclass fields
    # (e.g. iphone value_direction, browser url). Without it, a base-typed field drops
    # subclass-only fields on serialization (context.json in runner.py:285).
    action: SerializeAsAny[BaseAction] = Field(
        description="当前应该执行的唯一物理操作",
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
        if "action_type" in data and "action" not in data:
            return {"action": data}
        if isinstance(data.get("action"), str):
            nested = {k: v for k, v in data.items() if k != "action"}
            nested["action_type"] = data["action"]
            return {"action": nested}
        return data


class SupervisorStep(BaseModel):
    """Supervisor policy decision for one turn."""

    model_config = ConfigDict(extra="forbid")

    action_intent: Optional[ActionIntent] = Field(
        default=None,
        description="本帧唯一动作语义；None 表示终态、加载或非交互记录。",
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
    statement_id: Optional[str] = Field(default=None, description="当前子目标 ID")
    execution_scope: str = Field(
        default="",
        description=(
            "当前 statement invocation 的唯一记忆 scope："
            "<statement_instance_id>/statement。"
        ),
    )
    collection_scope: Optional[CollectionScope] = Field(default=None, description="当前内容采集范围")
    pre_existing: bool = Field(
        default=False,
        description="目标完成时，完成该里程碑的 action 由智能体执行（False）还是该状态在本次会话前就已存在（True）",
    )
    collection_summary: Optional[str] = Field(
        default=None,
        description="collection statement 完成时的采集摘要（含停止条件及触发原因）",
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
        if self.outcome is not None and (
            self.action_intent is not None or self.is_loading
        ):
            raise ValueError(
                "terminal SupervisorStep cannot request an action or loading wait"
            )
        if self.is_loading and self.action_intent is not None:
            raise ValueError("loading SupervisorStep cannot carry an ActionIntent")
        if self.preformed_action is not None and self.action_intent is None:
            raise ValueError("preformed action requires an ActionIntent")
        return self

class GoalValidationResult(BaseModel):
    """Result of independent goal-completion validation."""

    sufficient: bool = Field(description="已收集数据是否充分回答了用户目标")
    missing: str = Field(default="", description="缺少什么（sufficient=false 时填写）")


class CollectionIntent(BaseModel):
    """One phase of the collection state machine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["reach", "locate", "constrain"]
    entity: str = Field(min_length=1)
    required_fields: list[str] = Field(default_factory=list)
    field: str = "name"
    fallback: str = ""
    predicates: FilterPredicateSet = Field(default_factory=FilterPredicateSet)


InteractionIntent = Optional[CollectionIntent]

class StatementContract(BaseModel):
    """Frozen execution contract for one Program statement invocation input.

    Runtime status lives on StatementRuntimeState / StatementOutcome, never here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    goal: str
    success: str
    interaction_intent: InteractionIntent = None
    on: Literal["main"] = "main"
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    required_values: dict[str, JsonValue] = Field(default_factory=dict)
    observe_fields: list[str] = Field(default_factory=list)
    returns: dict[str, OutputSpec] = Field(default_factory=dict)
    persistence: PersistenceMode = "immediate"

class StatementInfo(BaseModel):
    """Persisted statement contract DTO written once per invocation (first turn)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = ""
    executor: Literal[
        "interact", "acquire", "read", "source_check", "compute", "command"
    ] = "interact"
    goal: str = ""
    success: str = ""
    interaction_intent: InteractionIntent = None
    on: Literal["main"] = "main"
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    required_values: dict[str, JsonValue] = Field(default_factory=dict)
    observe_fields: list[str] = Field(default_factory=list)
    persistence: PersistenceMode = "immediate"
    returns: dict[str, OutputSpec] = Field(default_factory=dict)

class StatementRuntimeSnapshot(BaseModel):
    """Minimal replay identity for an active statement invocation.

    Decision memory is rebuilt from Journal turns. No progress, constraint, or controller copy
    is checkpointed alongside the fact stream.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: StatementContract
    execution_scope: str = ""
    statement_info_emitted: bool = False
    task_type: TaskType = "action"
    initial_filters: Optional[dict[str, str]] = None


class TargetVerify(BaseModel):
    """Post-action targeting verify: did the snapped tap land on the intended element."""

    on_target: bool = Field(description="标记圆环是否正好落在指令意图的目标元素上")
    actual_element: str = Field(default="", description="标记实际落在的元素（如「转账 tab」「搜索框」）")
    reason: str = Field(default="", description="一句话理由")


CollectionBoundary = Literal["unknown", "at_end", "has_next_page", "not_at_end"]
CollectionSource = Literal["table", "viewport", "visual"]


class CollectionProvenance(BaseModel):
    """Stable identity of the observed collection, excluding observation time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_fingerprint: str = ""
    filter_snapshot: dict[str, JsonValue] = Field(default_factory=dict)
    schema_fingerprint: str = ""
    route: str = ""
    incomplete: bool = False


class CollectionSliceEvent(BaseModel):
    """One append-only normalized collection slice in the EventJournal.

    This is an observation fact, not an observe-decide-act turn. It deliberately carries
    neither a phase nor a next-action/completion decision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["collection_slice"] = "collection_slice"
    event_ref: str
    after_turn: int = Field(default=0, ge=0)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    statement_instance_id: str
    statement_id: str
    frame_ref: str
    collection_key: str
    provenance: CollectionProvenance
    window_key: str = ""
    content_key: str = ""
    records: list[dict[str, JsonValue]] = Field(default_factory=list)
    known_total: int | None = None
    boundary: CollectionBoundary = "unknown"
    source: CollectionSource = "table"
    strategy: Literal["structured", "react"] = "structured"
    truncated: bool = False


class AcquisitionReceiptEvent(BaseModel):
    """Append-only receipt for one acquisition capability probe or move.

    Receipts contain facts needed to rebuild Acquire decisions after replay.  They
    intentionally do not carry a writable phase, cursor or completion flag.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["acquisition_receipt"] = "acquisition_receipt"
    event_ref: str
    after_turn: int = Field(default=0, ge=0)
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    statement_instance_id: str
    statement_id: str
    collection_key: str = ""
    bound_region: str = ""
    strategy: Literal["structured", "react"]
    capability: str
    action_family: Literal[
        "bind_region", "paginate_next", "paginate_prev", "scroll_forward",
        "scroll_backward", "load_more", "wait",
    ]
    status: Literal["selected", "dispatched", "observed", "rejected", "failed"]
    before_content_key: str = ""
    after_content_key: str = ""
    reason: str = ""


class PolicyTurn(BaseModel):
    """One observe-decide-act turn saved in continue mode."""

    event_type: Literal["turn"] = "turn"
    index: int
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    operation_mode: Literal["interactive", "observation", "non_interactive"] = Field(
        default="interactive",
        description=(
            "本轮是 UI 交互执行、无动作观察仲裁，还是 Read/SourceCheck/Command 非交互执行"
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
    runtime_state: Optional[StatementRuntimeSnapshot] = Field(
        default=None,
        description="该 turn 收尾时的 statement 逻辑活态；仅用于 journal 重放恢复。",
    )
    supervisor: SupervisorStep
    action_decision: Optional[BaseActionDecision] = None
    non_ui: Optional[dict[str, Any]] = Field(
        default=None,
        description="非 UI primitive 执行明细：kind/sql/returns/reads/completed 等；interactive turn 留空",
    )
    transition: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "统一 Statement Transition 的 assessment、最终提议及机械校验结果。"
        ),
    )
    executed: bool = False
    action_signal: Optional[ActionSignal] = Field(
        default=None,
        description="动作派发、目标命中与页面响应的结构化信号。",
    )
    llm_calls: int = 0
    input_tokens: int = Field(default=0, description="本轮 LLM 调用累计输入 tokens（与 llm_calls 同口径），用于成本核算")
    output_tokens: int = Field(default=0, description="本轮 LLM 调用累计输出 tokens（与 llm_calls 同口径），用于成本核算")
    target_verify: Optional[TargetVerify] = Field(default=None, description="动作后落点校验：on_target, actual_element")
    timings: dict[str, float] = Field(default_factory=dict, description="各模块耗时(秒)，如 {transition: 1.2, action_policy: 2.3}")
    token_usage: dict[str, dict[str, int]] = Field(default_factory=dict, description="各模块 token 用量，如 {transition: {input: 2284, output: 114}}")
    settle_s: Optional[float] = Field(default=None, description="本轮动作后 settle 等待时长(秒)，等屏幕变过且停稳")
    no_effect: bool = Field(default=False, description="tap 类动作 settle 跑满上限且全程零变化：这一击对屏幕无效果（如重点已高亮 tab）")
    sections_loaded: list[str] = Field(
        default_factory=list,
        description="本轮 Transition 实际注入的渐进知识章节名；无渐进知识或未选中则为空",
    )
    llm_context: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "本轮 LLM 上下文诊断：实际 prompt 快照、context budget included/dropped blocks、"
            "估算 chars/tokens、block source/priority/ttl/裁剪原因，以及 KnowledgeSelector "
            "cache/fallback/section_ids。"
        ),
    )

    @model_validator(mode="after")
    def _reject_terminal_turn(self) -> "PolicyTurn":
        """Terminal outcomes belong in StatementOutcomeEvent, never an action turn."""
        if self.supervisor.outcome is not None:
            raise ValueError(
                "PolicyTurn cannot persist a terminal StatementOutcome; "
                "append StatementOutcomeEvent instead"
            )
        return self


class StatementOutcomeEvent(BaseModel):
    """Immutable terminal fact for exactly one statement invocation.

    A terminal verdict is not an observe-decide-act turn. Keeping it as its own
    journal event lets the next statement act on the same physical frame without
    creating an action-less turn or consuming turn/no-op budgets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["statement_outcome"] = "statement_outcome"
    after_turn: int = Field(
        default=0,
        ge=0,
        description="Number of persisted turns preceding this terminal fact.",
    )
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    observation_source: str = ""
    observation_url: str = ""
    statement: Optional[StatementInfo] = None
    statement_instance_id: str
    statement_id: str
    execution_scope: str = ""
    outcome: StatementOutcome
    transition: Optional[dict[str, Any]] = None
    pre_existing: bool = False
    collection_summary: Optional[str] = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    timings: dict[str, float] = Field(default_factory=dict)
    token_usage: dict[str, dict[str, int]] = Field(default_factory=dict)
    sections_loaded: list[str] = Field(default_factory=list)
    llm_context: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _strip_live_observation(cls, value: object) -> object:
        """Persist only the observation path, never raw screenshot bytes."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        outcome = data.get("outcome")
        if isinstance(outcome, StatementOutcome):
            if outcome.observation is not None:
                data["outcome"] = outcome.model_copy(update={"observation": None})
        elif isinstance(outcome, dict) and outcome.get("observation") is not None:
            serialized = dict(outcome)
            serialized["observation"] = None
            data["outcome"] = serialized
        return data


class ProgramRevisionEvent(BaseModel):
    """One immutable Program installation in the runtime revision history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["program_revision"] = "program_revision"
    revision: int
    action: Literal["start", "replace"]
    program: dict[str, Any]
    reason: str = ""
    terminal_disposition: Literal["none", "abandon", "record_then_drop"] = "none"
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class RecoveryJournalEvent(BaseModel):
    """One Program-level recovery fact suitable for budget replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: Literal["recovery"] = "recovery"
    recovery_class: str
    mechanism: str
    site: str
    detail: str = ""
    outcome: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


JournalEvent = Annotated[
    Union[
        PolicyTurn,
        CollectionSliceEvent,
        AcquisitionReceiptEvent,
        StatementOutcomeEvent,
        ProgramRevisionEvent,
        RecoveryJournalEvent,
    ],
    Field(discriminator="event_type"),
]


class EventJournal(BaseModel):
    """The single ordered fact stream for one Program execution.

    Turns, statement outcomes, Program revisions, collection facts, and recovery mechanisms
    share one ordered log.
    Post-dispatch sensors may finalize delivery fields on an existing PolicyTurn only through
    ``run.action_signals``; they never create a parallel ledger or rewrite outcome events.
    Runtime state and reports are rebuilt as projections from this persisted shape.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[4] = 4
    events: list[JournalEvent] = Field(default_factory=list)

    @property
    def turns(self) -> list[PolicyTurn]:
        return [event for event in self.events if isinstance(event, PolicyTurn)]

    @property
    def collection_slices(self) -> list[CollectionSliceEvent]:
        return [
            event for event in self.events
            if isinstance(event, CollectionSliceEvent)
        ]

    @property
    def acquisition_receipts(self) -> list[AcquisitionReceiptEvent]:
        return [
            event for event in self.events
            if isinstance(event, AcquisitionReceiptEvent)
        ]

    @property
    def program_revisions(self) -> list[ProgramRevisionEvent]:
        return [
            event for event in self.events if isinstance(event, ProgramRevisionEvent)
        ]

    @property
    def recovery_events(self) -> list[RecoveryJournalEvent]:
        return [event for event in self.events if isinstance(event, RecoveryJournalEvent)]

    @property
    def statement_outcomes(self) -> list[StatementOutcomeEvent]:
        return [
            event for event in self.events
            if isinstance(event, StatementOutcomeEvent)
        ]

    def append_turn(self, event: PolicyTurn) -> PolicyTurn:
        self.events.append(event)
        return event

    def append_collection_slice(
        self,
        event: CollectionSliceEvent,
    ) -> CollectionSliceEvent:
        if event.after_turn != len(self.turns):
            raise ValueError(
                "CollectionSliceEvent.after_turn must equal the current turn count"
            )
        if any(existing.event_ref == event.event_ref for existing in self.collection_slices):
            raise ValueError(f"duplicate collection event_ref {event.event_ref!r}")
        self.events.append(event)
        return event

    def append_acquisition_receipt(
        self,
        event: AcquisitionReceiptEvent,
    ) -> AcquisitionReceiptEvent:
        if event.after_turn != len(self.turns):
            raise ValueError(
                "AcquisitionReceiptEvent.after_turn must equal the current turn count"
            )
        if any(
            existing.event_ref == event.event_ref
            for existing in self.acquisition_receipts
        ):
            raise ValueError(f"duplicate acquisition event_ref {event.event_ref!r}")
        self.events.append(event)
        return event

    def append_statement_outcome(
        self,
        event: StatementOutcomeEvent,
    ) -> StatementOutcomeEvent:
        if event.after_turn != len(self.turns):
            raise ValueError(
                "StatementOutcomeEvent.after_turn must equal the current turn count"
            )
        if any(
            existing.statement_instance_id == event.statement_instance_id
            for existing in self.statement_outcomes
        ):
            raise ValueError(
                f"duplicate terminal outcome for {event.statement_instance_id!r}"
            )
        self.events.append(event)
        return event

    def append_program(
        self,
        program: dict[str, Any],
        *,
        action: Literal["start", "replace"],
        reason: str = "",
        terminal_disposition: Literal["none", "abandon", "record_then_drop"] = "none",
    ) -> ProgramRevisionEvent:
        event = ProgramRevisionEvent(
            revision=len(self.program_revisions) + 1,
            action=action,
            program=program,
            reason=reason,
            terminal_disposition=terminal_disposition,
        )
        self.events.append(event)
        return event

    def append_recovery(
        self,
        recovery_class: str,
        mechanism: str,
        site: str,
        *,
        detail: str = "",
        outcome: str = "",
    ) -> RecoveryJournalEvent:
        event = RecoveryJournalEvent(
            recovery_class=recovery_class,
            mechanism=mechanism,
            site=site,
            detail=detail,
            outcome=outcome,
        )
        self.events.append(event)
        return event


class PolicyContext(BaseModel):
    """Persistent context for multi-turn policy experiments."""

    goal: str
    supervisor_policy_name: str
    action_policy_name: str
    platform: Optional[str] = Field(
        default=None,
        description="运行平台 iphone/browser/android（AGENT_PLATFORM）。",
    )
    raw_input: Optional[str] = Field(
        default=None,
        description="用户/CLI 原始输入（temporal 解析、router 改写之前）。",
    )
    router: Optional[dict] = Field(
        default=None,
        description="RouterResult {goal, needs_clarification, clarification};bin/runner 直跑路径未经 router，为 None",
    )
    journal: EventJournal = Field(default_factory=EventJournal)
    task_type: Optional[TaskType] = None
    collection_scope: Optional[CollectionScope] = None
    outcome: Optional[ProgramOutcome] = None
    reply: Optional[str] = Field(
        default=None,
        description="前端基于 ProgramOutcome 生成并最终呈现给用户的回复。",
    )
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
        description="本次 run_agent_loop 端到端真实墙钟耗时（秒）；含 LLM、settle、感知/执行/调度等全部。",
    )
    orchestrator: Optional[dict] = Field(
        default=None,
        description="语义 Program 运行信息：{program, run_log, context_reports, token_usage}。"
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
