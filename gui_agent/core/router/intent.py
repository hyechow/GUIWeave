"""Resolve entity values and lookup hints before semantic Program compilation.

The resolver preserves user values, ranges and approximate-search hints. It does
not prescribe fields, query ladders, branches or UI routes; runtime statements
adapt those details against the real environment.
"""

from __future__ import annotations

from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator

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
    # (falls back to the supervisor model if unset), before orchestrator planning.
    cfg = resolve_llm_config("supervisor.intent")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )


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
    none of which bears on precise-vs-approximate. The orchestrator still receives the full knowledge
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
