"""Intent Resolver: an upfront semantic pass that classifies the entities a goal must look up.

Search-term relaxation does NOT belong in the agent's execution loop (reactive 'search exact → 0 →
relax' is uncertain and, as live run 20260622_124258 showed, often never even fires). Whether a
reference is PRECISE or APPROXIMATE is a property of the user's INTENT — decide it ONCE, upfront, and
let the orchestrator build a prioritized retrieval (exact first, fuzzy fallback) deterministically.

For each entity the goal needs to find in the system, this returns:
  - type:       what it is (product / customer / order / category / sku / review_text / generic) —
                lets the decomposer pick the right filter COLUMN (e.g. a product → the Product column,
                not the review-text column — the bug in 20260622_124258).
  - match_mode: exact (an order/SKU/id/email/@file value the user clearly copied verbatim) vs
                approximate (a product/person/title referred to in everyday words — these rarely
                string-match the canonical stored name; default named entities to approximate).
  - search_key: for approximate, the single most distinctive token likely to appear VERBATIM in the
                stored name (drop modifiers: 'Aurora jacket' → 'Aurora', because a broad phrase may
                not be a substring of the canonical stored name but the distinctive token can be).
                For exact, the full value.

The DECISION (this module): whether a reference is precise or approximate is intent — decided once,
upfront. That decision (permissiveness + search key) is rendered as a standalone, facts-only context
block via intent_block(), which decompose places right after the goal. The decomposer owns only the
retrieval STRATEGY (how to execute an allowed-fuzzy lookup: the exact→0→key ladder) — NOT the
whether-fuzzy decision. So intent and orchestration stay separate: the decision lives in this block,
the strategy lives in decomposer.py (rule 4b).

(A goal-text variant, annotate_goal(), was tried first and measured only ~1/3 decompose compliance —
a clause buried in goal prose reads as descriptive context and is easy for a 12k-char system prompt
to skip. intent_block(), as a separately-headed, priority-placed block, measured 100% over N=5. Kept
the dedicated-block approach; annotate_goal was removed.)"""

from __future__ import annotations

from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import target_value_options
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.router.intent_resolver")

_VALID_MODES = {"exact", "approximate"}


class EntityRef(BaseModel):
    """One entity the goal must look up in the target system."""

    mention: str = Field(description="目标原文里对该实体的引用,如 'Aurora jacket'")
    role: Literal[
        "lookup", "collection_scope", "target_value", "qualifier_value"
    ] = Field(
        default="lookup",
        description=(
            '"lookup"=检索既有命名实体;"collection_scope"=已命名实体下最终动作'
            '需覆盖的成员范围（不独立检索）;"target_value"=任务要引入或设置的'
            '目标值;"qualifier_value"=只限定最终选择的既有值。值角色保留原文且不检索'
        ),
    )
    value_members: list[str] = Field(
        default_factory=list,
        description=(
            "仅值角色使用：当 mention 表示同一逻辑选择必须包含的多个原子值时，"
            "逐项原样列出；标量值和不同字段的值留空并分别建 EntityRef。"
        ),
    )
    type: str = Field(default="generic", description="实体类型:product|customer|order|category|sku|review_text|generic")
    match_mode: str = Field(default="approximate", description='"exact"=系统级精确标识;"approximate"=口语/部分/转述引用')
    search_key: str = Field(default="", description="approximate:最显著、最可能逐字命中存储名称的【单个】token;exact:整串原值")
    cardinality: str = Field(default="single", description='"single"=指向唯一一个实体;"set"=一个规格,匹配多个实体(如"size 28 的所有颜色变体"、"所有蓝色 XS 商品"、"评分≤3 的所有评论")→ 下游须逐个迭代')
    selector: str = Field(default="", description='cardinality="set" 时,把成员从基底筛出来的规格/限定词(被 search_key 丢掉的那部分),如 "size 28"、"blue + size XS"、"rating<=3";single 时留空')
    reason: str = Field(default="", description="一句话依据")

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_role(cls, value: object) -> object:
        """Translate the retired value/introduction pair at the wire boundary."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        defaults = {
            "value_members": [],
            "type": "generic",
            "match_mode": "approximate",
            "search_key": "",
            "cardinality": "single",
            "selector": "",
            "reason": "",
        }
        for field, default in defaults.items():
            if data.get(field) is None:
                data[field] = default
        if data.get("role") == "value":
            data["role"] = (
                "qualifier_value"
                if data.get("introduction") == "not_required"
                else "target_value"
            )
        data.pop("introduction", None)
        return data

    @model_validator(mode="after")
    def _normalize_collection_scope(self) -> "EntityRef":
        """A logical coverage scope has no independent retrieval semantics."""
        if self.role == "collection_scope":
            self.cardinality = "set"
            self.match_mode = "exact"
            self.search_key = ""
            if not self.selector.strip():
                self.selector = self.mention
        return self


class IntentResolution(BaseModel):
    """The goal's entities, each classified precise-vs-fuzzy with a search key."""

    entities: list[EntityRef] = Field(default_factory=list)


def _llm() -> ChatOpenAI:
    # Text-only judgment on the goal — no screenshot. Configured under supervisor.intent
    # (falls back to the supervisor model if unset), parallel to supervisor.decompose.
    cfg = resolve_llm_config("supervisor.intent")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


def resolve_intent(
    goal: str,
    *,
    llm: Optional[ChatOpenAI] = None,
    trace_sink: Optional[list[dict]] = None,
) -> IntentResolution:
    """Classify the goal's lookup entities (precise vs fuzzy + search key). Empty when none.

    Text-only judgment on the goal — deliberately takes no app knowledge. The app's navigation
    knowledge (page map, UI operating details, filter formats) is HOW-layer content; it doesn't
    carry the kind of fact this judgment actually needs (whether an entity's stored form is exact
    or normalized away from how users refer to it). Feeding it in was pure noise on the prompt —
    measured: a knowledge.navigation excerpt led entirely with deployment/login/page-list info,
    none of which bears on precise-vs-approximate. decompose still receives the full knowledge
    independently (its own `knowledge` param) for the HOW it actually needs."""
    if not goal.strip():
        return IntentResolution()
    human = (
        f"用户目标:\n{goal}\n"
        "\n请抽取需要在系统中检索/定位的实体并分类;不需要检索的泛指词、动作、条件不要列。"
    )
    resolution = invoke_structured(
        llm or _llm(),
        [SystemMessage(content=_SYSTEM), HumanMessage(content=human)],
        IntentResolution,
        trace_sink=trace_sink,
        trace_label="orchestrator.intent",
    )
    # Normalize: clamp match_mode; default search_key to the mention.
    for e in resolution.entities:
        if e.role == "collection_scope":
            e.cardinality = "set"
            e.match_mode = "exact"
            e.search_key = ""
            if not e.selector.strip():
                e.selector = e.mention
            continue
        e.match_mode = e.match_mode.strip().lower()
        if e.match_mode not in _VALID_MODES:
            e.match_mode = "approximate"
        if not e.search_key.strip():
            e.search_key = e.mention
        e.value_members = list(target_value_options(e.value_members))
    return resolution


def intent_block(resolution: Optional[IntentResolution]) -> Optional[ContextBlock]:
    """Render the resolution as a standalone context block — FACTS ONLY (type/match_mode/
    search_key per entity), no strategy/orchestration prose. The decision (fuzzy allowed? which
    key?) rides as router-authoritative content; decompose's rule 4b owns translating it into
    ladder/column/success_condition steps. Placed right after task_goal_block (priority 21) so
    it's the first thing decompose reads. None when there are no entities."""
    if resolution is None or not resolution.entities:
        return None

    def _line(e: EntityRef) -> str:
        # Values are used verbatim and never searched. Their role says whether the task introduces
        # the value itself or merely selects it while mutating another object.
        if e.role in {"target_value", "qualifier_value"}:
            members = target_value_options(e.value_members)
            usage = (
                "｜目标要引入或设置该值"
                if e.role == "target_value"
                else "｜仅作为最终限定值，不得额外创建/改写其定义"
            )
            if len(members) > 1:
                return (
                    f"- 待填入同字段值集合「{e.mention}」｜类型={e.type}｜"
                    f"原子值={list(members)}（同一选择组，不合并成字符串）{usage}"
                )
            return f"- 待填入值「{e.mention}」｜类型={e.type}｜原样填写（不检索、不改拼写）{usage}"
        if e.role == "collection_scope":
            selector = (e.selector or e.mention).strip()
            return (
                f"- 成员覆盖范围「{e.mention}」｜类型={e.type}｜筛选/范围={selector}"
                "｜不是独立命名实体，禁止对该短语做 exact→fallback 检索；"
                "最终 mutation 必须用 foreach 逐成员覆盖，或在应用知识证明"
                "聚合 owner 一次覆盖时声明 covers_set"
            )
        match = ("允许模糊匹配，检索关键词：" + e.search_key) if e.match_mode == "approximate" else "精确匹配"
        # cardinality=set is authoritative for the DECISION (the reference denotes a SET, not one
        # entity) — but NOT for the strategy: HOW the set gets covered is decompose's call (foreach
        # per member, or a single aggregate action declared with covers_set when app knowledge says
        # one mechanism covers the whole group). Dictating "foreach" here overrode correct knowledge
        # (a parent record whose one save covers all members) and produced per-member mutations.
        if getattr(e, "cardinality", "single") == "set":
            sel = (getattr(e, "selector", "") or "").strip()
            card = (
                "｜**多目标(一组)**：这是一个规格，匹配多个实体"
                + (f"（筛选：{sel}）" if sel else "")
                + "，不可只处理其中一个就完事；覆盖方式二选一（规则 4c）："
                "foreach 逐成员处理，或当应用知识明确指出存在单一聚合对象/批量机制一次覆盖全组时，"
                "单步聚合动作并在该步声明 covers_set"
            )
        else:
            card = "｜单目标"
        return f"- 实体「{e.mention}」｜类型={e.type}｜{match}{card}"

    lines = [_line(e) for e in resolution.entities]
    target_values = [
        value
        for entity in resolution.entities
        if entity.role == "target_value"
        for value in (target_value_options(entity.value_members) or (entity.mention,))
    ]
    qualifier_values = [
        value
        for entity in resolution.entities
        if entity.role == "qualifier_value"
        for value in (target_value_options(entity.value_members) or (entity.mention,))
    ]
    if target_values or qualifier_values:
        lines.extend((
            "## 值生命周期合同",
            f"- 可作为任务写入目标、必要时建立定义前置：{target_values or ['（无）']}",
            f"- 只允许在最终 mutation 中选择、禁止建立独立定义阶段：{qualifier_values or ['（无）']}",
        ))
    return ContextBlock(
        id="runtime.intent_resolution",
        budget="required",
        source_type="runtime_state",
        source="intent_resolver",
        ttl="task",
        priority=21,  # right after task_goal_block(20) — first thing decompose reads
        content="## 实体检索语义（来源：意图解析）\n" + "\n".join(lines),
    )
