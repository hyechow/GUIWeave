"""Small, explicit context blocks for prompt assembly.

Prompt assets define stable task instructions. Context blocks carry the runtime
and knowledge snippets that are assembled around those instructions, with enough
metadata for the model and logs to see where each snippet came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping


# Budget tiers, drop-eagerness ascending. `required` is never dropped (task goal, current
# acceptance gate, guard feedback, @file data — dropping these silently corrupts the task).
# Lower tiers are shed first when the assembled context exceeds the char ceiling.
BUDGET_TIERS = ("low", "medium", "high", "required")
_BUDGET_TIER_RANK = {tier: rank for rank, tier in enumerate(BUDGET_TIERS)}
# Within a tier, drop the STALEST first — keep the current turn's live observations (page
# title, current page identity, loop summary) over reusable session/task state (history,
# knowledge). Higher rank = dropped earlier; turn-scoped (freshest) is dropped last.
_TTL_DROP_RANK = {"task": 0, "session": 1, "turn": 2}


@dataclass(frozen=True)
class ContextBlock:
    """One prompt context fragment with source metadata."""

    id: str
    source_type: str
    source: str
    content: str
    priority: int = 100
    ttl: str = "turn"
    # Budget tier (see BUDGET_TIERS): which blocks the ContextBudgeter may drop under a hard
    # char ceiling. Producers tag load-bearing blocks `required`; default is droppable `medium`.
    budget: str = "medium"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Signal-source authority (see context/signal_source_architecture.md). Which claim/domains this
    # block is authoritative for, and (optionally) explicitly NOT — so a source can't be read
    # outside its authority. freshness/coverage let the model reconcile conflicts without guessing.
    authoritative_for: tuple[str, ...] = ()
    not_authoritative_for: tuple[str, ...] = ()
    freshness: str = ""      # turn | post_action | prior_turn | task_static
    coverage: str = ""       # complete | rendered_only | partial | unknown

    def render(self, *, include_header: bool = True) -> str:
        body = (self.content or "").strip()
        if not include_header:
            return body
        parts = [
            f"context: {self.id}",
            f"type={self.source_type}",
            f"source={self.source}",
        ]
        if self.authoritative_for:
            parts.append(f"authoritative_for={','.join(self.authoritative_for)}")
        if self.not_authoritative_for:
            parts.append(f"not_authoritative_for={','.join(self.not_authoritative_for)}")
        if self.freshness:
            parts.append(f"freshness={self.freshness}")
        if self.coverage:
            parts.append(f"coverage={self.coverage}")
        if self.ttl:
            parts.append(f"ttl={self.ttl}")
        for key in sorted(self.metadata):
            value = self.metadata[key]
            if value is None or value == "":
                continue
            parts.append(f"{key}={_format_metadata_value(value)}")
        header = "[" + " | ".join(parts) + "]"
        return f"{header}\n{body}" if body else header


@dataclass(frozen=True)
class ContextBundle:
    """An ordered group of context blocks."""

    blocks: tuple[ContextBlock, ...] = ()

    def render(
        self,
        *,
        include_headers: bool = True,
        sort_by_priority: bool = False,
        separator: str = "\n\n",
    ) -> str:
        blocks = self.blocks
        if sort_by_priority:
            blocks = tuple(
                block for _, block in sorted(
                    enumerate(blocks),
                    key=lambda item: (item[1].priority, item[0]),
                )
            )
        return separator.join(
            block.render(include_header=include_headers)
            for block in blocks
            if (block.content or "").strip()
        )


def render_context_blocks(
    blocks: Iterable[ContextBlock | None],
    *,
    include_headers: bool = True,
    sort_by_priority: bool = False,
    separator: str = "\n\n",
) -> str:
    """Render non-empty blocks with stable ordering."""
    bundle = ContextBundle(tuple(block for block in blocks if block is not None))
    return bundle.render(
        include_headers=include_headers,
        sort_by_priority=sort_by_priority,
        separator=separator,
    )


@dataclass(frozen=True)
class BudgetResult:
    """Outcome of applying a char ceiling to a set of context blocks."""

    text: str
    kept: tuple[ContextBlock, ...]
    dropped: tuple[ContextBlock, ...]
    kept_chars: int
    # True when the `required` blocks alone already exceed the ceiling — nothing droppable is
    # left, so the result is over budget by design (required blocks are never dropped).
    over_budget: bool
    max_chars: int = 0
    total_chars: int = 0
    dropped_chars: int = 0
    decisions: tuple["ContextBlockDecision", ...] = ()

    @property
    def estimated_tokens(self) -> int:
        return _estimate_tokens(self.total_chars)

    @property
    def kept_tokens(self) -> int:
        return _estimate_tokens(self.kept_chars)

    def to_report(self, *, label: str = "context") -> dict[str, Any]:
        """Serialize the budget decision for run reports."""
        blocks = [decision.to_dict() for decision in self.decisions]
        included = [block for block in blocks if block.get("included")]
        dropped = [block for block in blocks if not block.get("included")]
        return {
            "kind": "context_budget",
            "label": label,
            "max_chars": self.max_chars,
            "estimated_chars": self.total_chars,
            "estimated_tokens": self.estimated_tokens,
            "kept_chars": self.kept_chars,
            "kept_tokens": self.kept_tokens,
            "dropped_chars": self.dropped_chars,
            "over_budget": self.over_budget,
            "included_count": len(included),
            "dropped_count": len(dropped),
            "included": included,
            "dropped": dropped,
            "blocks": blocks,
        }


@dataclass(frozen=True)
class ContextBlockDecision:
    """Report row for one context block after budget selection."""

    id: str
    source_type: str
    source: str
    priority: int
    ttl: str
    budget: str
    chars: int
    included: bool
    reason: str
    truncation_reason: str

    @property
    def estimated_tokens(self) -> int:
        return _estimate_tokens(self.chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "source": self.source,
            "priority": self.priority,
            "ttl": self.ttl,
            "budget": self.budget,
            "estimated_chars": self.chars,
            "estimated_tokens": self.estimated_tokens,
            "included": self.included,
            "reason": self.reason,
            "truncation_reason": self.truncation_reason,
        }


class ContextBudgeter:
    """Hard char ceiling for assembled context blocks — DROP-ONLY (v1).

    No compression (truncating a block body risks feeding a half-config / half-rule = silent
    corruption) and NO reordering (prompt order affects model behavior; render order is
    preserved, blocks are only removed). When the total estimated size exceeds ``max_chars``,
    droppable blocks are shed lowest-tier-first (low → medium → high), then most-transient ttl,
    then oldest, until it fits. ``required`` blocks are never dropped. Sizing is a char count
    for v1; pass ``estimate`` to swap in a real tokenizer later."""

    def __init__(
        self,
        max_chars: int,
        *,
        include_headers: bool = True,
        estimate: Callable[[ContextBlock], int] | None = None,
    ) -> None:
        self.max_chars = max_chars
        self.include_headers = include_headers
        self._estimate = estimate or (lambda b: len(b.render(include_header=include_headers)))

    def apply(self, blocks: Iterable[ContextBlock | None]) -> BudgetResult:
        live = [b for b in blocks if b is not None and (b.content or "").strip()]
        idx = {id(b): i for i, b in enumerate(live)}
        sizes = {id(b): self._estimate(b) for b in live}
        total = sum(sizes.values())
        dropped_ids: set[int] = set()
        if total > self.max_chars:
            order = sorted(
                (b for b in live if b.budget != "required"),
                key=lambda b: (
                    _BUDGET_TIER_RANK.get(b.budget, _BUDGET_TIER_RANK["medium"]),
                    _TTL_DROP_RANK.get(b.ttl, 0),
                    idx[id(b)],
                ),
            )
            cur = total
            for b in order:
                if cur <= self.max_chars:
                    break
                dropped_ids.add(id(b))
                cur -= sizes[id(b)]
        kept = tuple(b for b in live if id(b) not in dropped_ids)
        dropped = tuple(b for b in live if id(b) in dropped_ids)
        kept_chars = sum(sizes[id(b)] for b in kept)
        dropped_chars = sum(sizes[id(b)] for b in dropped)
        decisions = tuple(
            ContextBlockDecision(
                id=b.id,
                source_type=b.source_type,
                source=b.source,
                priority=b.priority,
                ttl=b.ttl,
                budget=b.budget,
                chars=sizes[id(b)],
                included=id(b) not in dropped_ids,
                reason=_decision_reason(b, id(b) in dropped_ids, total > self.max_chars),
                truncation_reason=(
                    "dropped_over_budget" if id(b) in dropped_ids else "not_truncated"
                ),
            )
            for b in live
        )
        return BudgetResult(
            text=render_context_blocks(kept, include_headers=self.include_headers),
            kept=kept,
            dropped=dropped,
            kept_chars=kept_chars,
            over_budget=kept_chars > self.max_chars,
            max_chars=self.max_chars,
            total_chars=total,
            dropped_chars=dropped_chars,
            decisions=decisions,
        )


def _format_metadata_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _estimate_tokens(chars: int) -> int:
    """Cheap report-only token estimate until call sites have tokenizer access."""
    if chars <= 0:
        return 0
    return max(1, (chars + 3) // 4)


def _decision_reason(block: ContextBlock, dropped: bool, was_over_budget: bool) -> str:
    if dropped:
        return f"dropped: over budget; tier={block.budget}; ttl={block.ttl}; priority={block.priority}"
    if block.budget == "required":
        return "included: required"
    if was_over_budget:
        return f"included: survived budget; tier={block.budget}; ttl={block.ttl}; priority={block.priority}"
    return "included: within budget"
