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

    def render(self, *, include_header: bool = True) -> str:
        body = (self.content or "").strip()
        if not include_header:
            return body
        parts = [
            f"context: {self.id}",
            f"type={self.source_type}",
            f"source={self.source}",
        ]
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
        return BudgetResult(
            text=render_context_blocks(kept, include_headers=self.include_headers),
            kept=kept,
            dropped=dropped,
            kept_chars=kept_chars,
            over_budget=kept_chars > self.max_chars,
        )


def _format_metadata_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)
