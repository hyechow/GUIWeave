from dataclasses import dataclass

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from gui_agent.core.schemas import ActionFamily, AtomicRole, CollectionScope, StatementContract


def action_metadata(plan, statement: StatementContract) -> tuple[AtomicRole, ActionFamily]:
    """Normalize planner metadata against the statement execution strategy."""
    if statement.is_iterative:
        return "iterate", "iterate"
    return plan.atomic_role, plan.action_family


@dataclass(frozen=True)
class StatementPrompts:
    """Platform-specific LLM prompt set for the statement supervisor.

    The supervisor FRAMEWORK (policy.py check→plan loop + model I/O) is
    platform-neutral; adapters own the prompt bundle and load the model-visible
    bodies from Markdown prompt assets. This container is the neutral seam: only
    the field SHAPE lives in core, never the content. Fields cover every prompt
    policy.py + model_io.py consume."""

    single_checker: str
    check_kind_sections: dict
    check_section_default: str
    check_section_converge: str
    loop_frame: str
    plan: str
    loop_scroll: str
    replan: str
    # Prompt-side image preprocessing. iPhone screenshots are captured at Retina
    # scale and should be halved before vision calls; Android/browser screenshots
    # are already in the coordinate space the agent reasons about.
    image_resize: Literal["retina", "none"] = "retina"
    # Optional structured-output schema for the step planner. Platforms that do not
    # use picker fields can provide a smaller schema, so those fields are not shown
    # to the model.
    plan_result_schema: type[BaseModel] | None = None
    # Optional override for the KnowledgeSelector prompt (model_io.run_selector). The
    # selection task (match current page + statement against a section-title list) is
    # platform-neutral, so the core default in model_io.py fits all platforms; override
    # only if a platform needs different selection guidance.
    selector: str | None = None
    # Platform-owned page_identity markers that mean "system home/launcher screen".
    # Core only applies the configured markers; it does not know iOS/Android/browser
    # home-screen vocabulary itself.
    home_identity_markers: tuple[str, ...] = ()

    @classmethod
    def neutral(cls) -> "StatementPrompts":
        """Return a platform-neutral bundle for deterministic execution and tooling."""
        return cls(
            single_checker="Verify the current statement from supplied observations and contracts.",
            check_kind_sections={},
            check_section_default="Use visible evidence and structured state only.",
            check_section_converge="Report current and target values for iterative controls.",
            loop_frame="Assess collection progress and the observable boundary.",
            plan="Propose one atomic action using structured target metadata.",
            loop_scroll="Propose one collection-progress action.",
            replan="Propose one local recovery action without changing the goal.",
            image_resize="none",
        )


def _coerce_str_list(value):
    """Tolerate an LLM returning a ``list[str]`` field as a bare string (or None).

    DashScope ``json_object`` mode occasionally emits ``{"missing_evidence": "需要…"}``
    instead of a list. Without this the primary ``model_validate`` raises
    ``ValidationError`` and ``invoke_structured`` falls back to a slow plain-text
    reparse (1-2 extra LLM calls — see log 20260616_200258 Turn5/6, checker=10.58s).
    Wrap a string into a single-element list, stringify non-str list elements, coerce
    None to []."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return value


class _ChecklistVerdict(BaseModel):
    """Per-item verdict for one enumerated acceptance sub-condition."""
    index: int = Field(description="对应「逐项验收」清单里的序号（从 1 开始）")
    met: bool = Field(description="该子项是否已满足")
    evidence: str = Field(default="", description="支持该判定的一句可见证据")


class _SingleCheckResult(BaseModel):
    """Checker output for single-step statements (navigation/filter/action/verification/read_once).

    LLM checker should only return done or in_progress.
    stuck is reserved for programmatic checks (screen similarity, instruction repetition).
    """
    status: Literal["done", "in_progress", "stuck"] = Field(
        description="判断状态：done（验收通过）或 in_progress（未完成）。禁止填 'loading'——页面加载状态用独立的 loading 布尔字段表示"
    )
    reason: str = Field(description="判断理由")
    item_verdicts: list[_ChecklistVerdict] = Field(
        default_factory=list,
        description="逐项验收：对 prompt「逐项验收」段里每个编号子项独立判 met+证据；整体 status 仍按综合判断填。无该段时留空。",
    )
    stuck_reason: str = Field(default="", description="额外未达成原因；一般验收判断留空")
    issues: list[str] = Field(default_factory=list)
    visible_evidence: list[str] = Field(default_factory=list, description="截图中支持 done 的可见证据")
    missing_evidence: list[str] = Field(default_factory=list, description="缺失的验收证据")
    page_identity: str = Field(default="", description="当前页面/视图的身份识别（如：列表页、详情页、设置页）")
    summary: str = Field(description="当前屏幕状态一句话描述")
    read_instruction: Optional[str] = Field(
        default=None,
        description="kind=collection(read_once) 或 kind=verification 时填写：当前屏幕需要提取的内容说明；其他类型留空",
    )
    frozen: bool = Field(default=False, description="屏幕是否冻结（相似度≥99%，即使 reader 返回新内容也应停止）")
    loading: bool = Field(default=False, description="页面正在加载（骨架屏/启动屏/转场动画），应等待下一帧而非立即规划动作")
    effect_status: Literal["confirmed", "unmet", "rejected", "unverified"] = Field(
        description=(
            "业务目标状态：confirmed=目标状态已有证据；unmet=目标当前尚未满足，仍需正常执行；"
            "rejected=本子目标动作派发后出现明确错误、拒绝或可靠后置反证；"
            "unverified=当前帧没有可判读的目标结果通道。不得用动作已派发替代 confirmed。"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_verdict_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "effect_status" not in data and "outcome_status" in data:
            data["effect_status"] = data.pop("outcome_status")
        # Historical ``contradicted`` conflated a normal unmet target with an explicit
        # post-action rejection.  The safe compatibility interpretation is unmet; current
        # checkers use ``rejected`` only when a dispatched action has concrete failure evidence.
        if data.get("effect_status") == "contradicted":
            data["effect_status"] = "unmet"
        # Tolerate an omitted ``summary`` (a display field): default to "" so a checker that
        # drops it still parses on the primary json_object pass instead of triggering the slow
        # plain-text reparse. ``effect_status`` is deliberately NOT inferred — it is the
        # business-target verdict and must be stated explicitly; omission falls back to reparse
        # so the model re-answers with an honest verdict (see
        # test_checker_payload_requires_explicit_effect_status).
        if "summary" not in data:
            data["summary"] = ""
        return data

    @field_validator("issues", "visible_evidence", "missing_evidence", mode="before")
    @classmethod
    def _coerce_str_list_fields(cls, v):
        return _coerce_str_list(v)


class _SelectorResult(BaseModel):
    """KnowledgeSelector output: which knowledge sections the upcoming planner should read.

    A dedicated micro-decision (text-only, cached per (statement, page)) — the checker no
    longer moonlights as a retriever. Ids come from the ``[sNN]`` manifest, so resolution
    back to files is an exact lookup."""
    section_ids: list[str] = Field(
        default_factory=list,
        description="与当前页面/下一步操作最相关的 1~3 个章节 ID（照抄清单里方括号内的 ID，如 s07）；没有相关章节就返回空列表",
    )
    reason: str = Field(default="", description="选择依据（一句话）")

    @field_validator("section_ids", mode="before")
    @classmethod
    def _coerce_section_ids(cls, v):
        return _coerce_str_list(v)


class _LoopFrameResult(BaseModel):
    """Per-frame assessment for scroll_until_boundary statements."""
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
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = Field(
        default="prepare",
        description=(
            "当前原子动作角色：prepare=展开/定位/导航等获取动作；write=输入/选择/切换目标值；"
            "commit=提交/保存/发送等最终副作用边界；iterate=滚动/picker 等有进展时可重复动作。"
            "不要填 navigate/activate/input/select——那些属于 action_family。"
        ),
    )
    action_family: Literal[
        "input", "select", "activate", "navigate", "iterate", "unknown"
    ] = Field(
        default="unknown",
        description=(
            "本轮指令的动作族：input=输入/清空，select=选择值，activate=点击普通控件，"
            "navigate=页面/标签跳转，iterate=滚动/拖动，unknown=无法判断。保存/提交的"
            "事务语义只由 atomic_role=commit 表达；点击保存按钮时 family 仍为 activate。"
        ),
    )

    target_control: str = Field(
        default="",
        description=(
            "本轮原子动作要命中的具体控件、字段或集合能力；用于动作定位，不是 statement "
            "终态字段的字面白名单。"
        ),
    )
    target_value: str = Field(
        default="",
        description="本轮写入/选择的结构化目标值；不要依赖 instruction 文本重新抽取。",
    )
    direction: Optional[Literal["up", "down", "left", "right", "increase", "decrease"]] = Field(
        default=None,
        description=(
            "普通列表 scroll 时填手指移动方向（up/down/left/right）；"
            "picker drag/scroll 时填值的变化方向（increase=值变大，decrease=值变小）；"
            "tap/type/home 留空"
        ),
    )
    # Picker-wheel fields: iPhone date pickers use year/month/day; Android time
    # pickers use period/hour/minute. Platforms without pickers leave them None.
    drag_column: Optional[str] = Field(
        default=None,
        description=(
            "picker drag/scroll 时的目标列，如 'year'/'month'/'day' 或 "
            "'period'/'hour'/'minute'；非 picker 操作留空"
        ),
    )
    drag_current_value: Optional[int] = Field(
        default=None,
        description=(
            "picker drag/scroll 时，要拖的【那一列】当前停在中间行的数字（从 check_reason 读出，"
            "如日列当前为 5月1日就填 1、分钟列当前 52 就填 52；上午/下午列用 上午=0、下午=1）；"
            "非 picker 操作留空。"
            "它与 drag_target_value 一起让系统按差几格自动放大拖动幅度——少填会退化成一格一格挪。"
        ),
    )
    drag_target_value: Optional[int] = Field(
        default=None,
        description=(
            "picker drag/scroll 时，要拖的【那一列】的目标数字（如目标 5月21日、本步拖日列就填 21；"
            "目标为上午则填 0、下午则填 1）；非 picker 操作留空。必须与 drag_current_value 取同一列的数字。"
        ),
    )


class _ReplanResult(BaseModel):
    diagnosis: str = Field(description="当前未达成目标的原因（一句话）")
    strategy: Literal["local_replan", "escalate_human", "force_complete"]
    instruction: str = Field(default="")
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = Field(
        default="prepare",
        description=(
            "修复指令的原子执行角色；语义与正常 planner 输出一致。"
            "不要填 navigate/activate/input/select——那些属于 action_family。"
        ),
    )
    action_family: Literal[
        "input", "select", "activate", "navigate", "iterate", "unknown"
    ] = Field(
        default="unknown",
        description=(
            "修复指令的 UI 原语族；不得只在 instruction 文本中隐含。保存/提交使用"
            "atomic_role=commit，点击原语仍填 activate。"
        ),
    )
    target_control: str = Field(
        default="",
        description="修复动作要命中的具体控件；用于 adapter grounding，不作终态字段的字面白名单。",
    )
    target_value: str = Field(
        default="",
        description="修复动作要写入或选择的值；非写入动作留空。",
    )
    direction: Optional[
        Literal["up", "down", "left", "right", "increase", "decrease"]
    ] = None
    drag_column: Optional[str] = None
    drag_current_value: Optional[int] = None
    drag_target_value: Optional[int] = None
    escalation_message: str = Field(default="")
