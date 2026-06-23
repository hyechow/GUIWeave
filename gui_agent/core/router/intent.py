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
    lines = [
        f"- 实体「{e.mention}」｜类型={e.type}｜{'允许模糊匹配，检索关键词：' + e.search_key if e.match_mode == 'approximate' else '精确匹配'}"
        for e in resolution.entities
    ]
    return ContextBlock(
        id="runtime.intent_resolution",
        budget="required",
        source_type="runtime_state",
        source="intent_resolver",
        ttl="task",
        priority=21,  # right after task_goal_block(20) — first thing decompose reads
        content="## 实体检索语义（来源：意图解析）\n" + "\n".join(lines),
    )
