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

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.router.intent_resolver")

_VALID_MODES = {"exact", "approximate"}


class EntityRef(BaseModel):
    """One entity the goal must look up in the target system."""

    mention: str = Field(description="目标原文里对该实体的引用,如 'Aurora jacket'")
    role: str = Field(default="lookup", description='"lookup"=要在系统里检索/定位的既有实体;"value"=要设置/创建/填写的值(新名称、表单选项、规则作用域)——原样使用,不检索、绝不改拼写')
    type: str = Field(default="generic", description="实体类型:product|customer|order|category|sku|review_text|generic")
    match_mode: str = Field(default="approximate", description='"exact"=系统级精确标识;"approximate"=口语/部分/转述引用')
    search_key: str = Field(default="", description="approximate:最显著、最可能逐字命中存储名称的【单个】token;exact:整串原值")
    cardinality: str = Field(default="single", description='"single"=指向唯一一个实体;"set"=一个规格,匹配多个实体(如"size 28 的所有颜色变体"、"所有蓝色 XS 商品"、"评分≤3 的所有评论")→ 下游须逐个迭代')
    selector: str = Field(default="", description='cardinality="set" 时,把成员从基底筛出来的规格/限定词(被 search_key 丢掉的那部分),如 "size 28"、"blue + size XS"、"rating<=3";single 时留空')
    reason: str = Field(default="", description="一句话依据")


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
        e.match_mode = e.match_mode.strip().lower()
        if e.match_mode not in _VALID_MODES:
            e.match_mode = "approximate"
        if not e.search_key.strip():
            e.search_key = e.mention
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
        # role=value: a value to be SET/CREATED (a new rule name, a form option, a scope setting) —
        # used verbatim, never searched. Rendering it as a retrieval line made decompose search for
        # things that don't exist yet (703 "Thanks giving sale") or foreach over form settings (702).
        if getattr(e, "role", "lookup") == "value":
            return f"- 待填入值「{e.mention}」｜类型={e.type}｜原样填写（不检索、不改拼写）"
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
    return ContextBlock(
        id="runtime.intent_resolution",
        budget="required",
        source_type="runtime_state",
        source="intent_resolver",
        ttl="task",
        priority=21,  # right after task_goal_block(20) — first thing decompose reads
        content="## 实体检索语义（来源：意图解析）\n" + "\n".join(lines),
    )
