"""Transition-specific variants offered to the shared ContextCompressor."""

from __future__ import annotations

from copy import deepcopy
import json

from gui_agent.context import ContextBlock, ContextVariant


def transition_frame_block(frame: dict) -> ContextBlock:
    """Return the full frame with an optional affordance-compressed variant."""
    full_frame = _prepare_frame(frame, drop_background=False)
    compact_frame = _prepare_frame(frame, drop_background=True)
    full_content = _render(full_frame)
    compact_content = _render(compact_frame)
    variants = ()
    if len(compact_content) < len(full_content):
        variants = (ContextVariant(
            strategy="drop_background_offscreen_affordances",
            content=compact_content,
            priority=10,
            reason=(
                "retain current, contract-target and supporting affordances; "
                "drop background offscreen affordances"
            ),
        ),)
    return ContextBlock(
        id="runtime.transition_frame",
        budget="required",
        source_type="decision_frame",
        source="journal+observation+contract",
        ttl="turn",
        priority=20,
        content=full_content,
        variants=variants,
    )


def _prepare_frame(frame: dict, *, drop_background: bool) -> dict:
    prepared = deepcopy(frame)
    observation = prepared.get("observation")
    affordances = observation.get("affordances") if isinstance(observation, dict) else None
    if not isinstance(affordances, list):
        return prepared
    visible: list = []
    dropped = False
    for item in affordances:
        if not isinstance(item, dict):
            visible.append(item)
            continue
        relevance = item.pop("_relevance", "background")
        if drop_background and relevance == "background":
            dropped = True
            continue
        visible.append(item)
    observation["affordances"] = visible
    if dropped:
        observation["affordance_coverage"] = "partial"
    return prepared


def _render(frame: dict) -> str:
    return (
        "## TransitionFrame（本帧唯一决策包）\n"
        "以下是合同、Journal 事实和当前观察；其中没有预先计算的完成状态或路线。\n"
        + json.dumps(
            frame,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


__all__ = ["transition_frame_block"]
