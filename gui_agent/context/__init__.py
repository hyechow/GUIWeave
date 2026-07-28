"""Prompt-context composition primitives."""

from .blocks import (
    BUDGET_TIERS,
    ContextBlock,
    ContextBlockDecision,
    ContextBundle,
    ContextCompressionResult,
    ContextCompressor,
    ContextVariant,
    render_context_blocks,
)

__all__ = [
    "BUDGET_TIERS",
    "ContextBlock",
    "ContextBlockDecision",
    "ContextBundle",
    "ContextCompressionResult",
    "ContextCompressor",
    "ContextVariant",
    "render_context_blocks",
]
