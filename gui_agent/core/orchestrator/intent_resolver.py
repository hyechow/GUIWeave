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
                stored name (drop modifiers: 'Olivia zip jacket' → 'Olivia', because the full phrase
                is not a substring of 'Olivia 1/4 Zip Light Jacket' but 'Olivia' is). For exact, the
                full value.

The decomposer consumes this to build, per approximate entity, a prioritized filter milestone:
try the exact mention first; if it returns 0, retry with search_key — with a result-requiring
success_condition so 0 records is NOT accepted as done. This pure judgment is the module; the
decomposer wiring lives in decomposer.py."""

from __future__ import annotations

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.orchestrator.intent_resolver")

_VALID_MODES = {"exact", "approximate"}


class EntityRef(BaseModel):
    """One entity the goal must look up in the target system."""

    mention: str = Field(description="目标原文里对该实体的引用,如 'Olivia zip jacket'")
    type: str = Field(default="generic", description="实体类型:product|customer|order|category|sku|review_text|generic")
    match_mode: str = Field(default="approximate", description='"exact"=系统级精确标识;"approximate"=口语/部分/转述引用')
    search_key: str = Field(default="", description="approximate:最显著、最可能逐字命中存储名称的【单个】token;exact:整串原值")
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
    app_knowledge: str = "",
    llm: Optional[ChatOpenAI] = None,
    trace_sink: Optional[list[dict]] = None,
) -> IntentResolution:
    """Classify the goal's lookup entities (precise vs fuzzy + search key). Empty when none."""
    if not goal.strip():
        return IntentResolution()
    human = f"用户目标:\n{goal}\n"
    if app_knowledge.strip():
        human += f"\n应用知识(可选,帮助判断实体类型/存储形态):\n{app_knowledge[:1500]}\n"
    human += "\n请抽取需要在系统中检索/定位的实体并分类;不需要检索的泛指词、动作、条件不要列。"
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
    """Render the resolution as a high-priority decompose context block (authoritative for how to
    search). None when there are no entities."""
    if resolution is None or not resolution.entities:
        return None
    lines = []
    for e in resolution.entities:
        if e.match_mode == "approximate":
            how = f"近似引用 → 先用精确值「{e.mention}」检索,若 0 条改用关键词「{e.search_key}」模糊重检索"
        else:
            how = f"精确标识 → 直接用「{e.mention}」精确检索"
        lines.append(f"- 实体「{e.mention}」｜类型={e.type}｜{how}")
    return ContextBlock(
        id="runtime.intent_resolution",
        budget="required",
        source_type="runtime_state",
        source="intent_resolver",
        ttl="task",
        priority=16,
        content=(
            "## 实体检索意图(来源:意图解析｜权威:高于默认习惯)\n"
            "据此为每个实体编排检索 milestone:按【类型】选对应筛选列(如 product→Product 列,不是评论文本列);\n"
            "按【精确/近似】决定检索方式(近似实体编成优先级阶梯:先精确、0 条再模糊),并把该 milestone 的\n"
            "success_condition 写成『已检索到匹配该实体的记录(非 0 条)』——0 条不算完成。\n"
            + "\n".join(lines)
        ),
    )
