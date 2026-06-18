"""Small, explicit context blocks for prompt assembly.

Prompt assets define stable task instructions. Context blocks carry the runtime
and knowledge snippets that are assembled around those instructions, with enough
metadata for the model and logs to see where each snippet came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class ContextBlock:
    """One prompt context fragment with source metadata."""

    id: str
    source_type: str
    source: str
    content: str
    priority: int = 100
    ttl: str = "turn"
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
            parts.append(f"{key}={value}")
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
