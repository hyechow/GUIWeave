"""Small runtime helpers shared by the milestone supervisor."""

from __future__ import annotations

import time
from typing import Optional

from llm.structured import get_llm_token_usage
from gui_agent.core.schemas import Milestone, PolicyTurn

MAX_RETRIES = 3
# Consult the Feasibility Guard ONCE this early (before the MAX_RETRIES give-up): a milestone that
# is already stuck twice is worth an infeasibility check now, not after it exhausts all retries. The
# judge is conservative-toward-feasible, so an early probe that says "feasible" just continues.
EARLY_FEASIBILITY_AT = 2
MAX_SCROLL_PER_MILESTONE = 3

class _Timer:
    """Context manager: time a named section and optionally record token deltas."""

    def __init__(
        self,
        collector: dict[str, float],
        order: list[str],
        name: str,
        tokens: dict[str, dict[str, int]] | None = None,
    ):
        self._c = collector
        self._o = order
        self._n = name
        self._s = 0.0
        self._tokens = tokens
        self._tok0 = (0, 0)

    def __enter__(self):
        self._s = time.perf_counter()
        if self._tokens is not None:
            self._tok0 = get_llm_token_usage()
        return self

    def __exit__(self, *a):
        d = time.perf_counter() - self._s
        if self._n not in self._c:
            self._o.append(self._n)
        self._c[self._n] = self._c.get(self._n, 0) + d
        if self._tokens is not None:
            inp, out = get_llm_token_usage()
            slot = self._tokens.setdefault(self._n, {"input": 0, "output": 0})
            slot["input"] += inp - self._tok0[0]
            slot["output"] += out - self._tok0[1]


def _is_loop(milestone: Milestone) -> bool:
    return (
        milestone.kind == "collection"
        and milestone.completion_strategy == "scroll_until_boundary"
    )


def _last_scroll_was_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return bool(
        history
        and history[-1].supervisor.milestone_id == milestone_id
        and history[-1].action_decision
        and history[-1].action_decision.action
        and history[-1].action_decision.action.action_type == "scroll"
        and history[-1].executed
    )


def _has_successful_scroll_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id
        and t.action_decision
        and t.action_decision.action
        and t.action_decision.action.action_type in {"scroll", "drag"}
        and t.executed
        for t in history
    )


def _has_collected(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id and t.read_added_content
        for t in history
    )


def _default_read_instruction(milestone: Milestone) -> str:
    return (
        f"提取当前屏幕中与「{milestone.name}」相关的所有可见内容，"
        "保留名称/标题、时间/位置、目标相关数值、状态、类别等字段；如果是列表，逐条提取。"
    )


def _ctx(milestone: Milestone, read_instruction: Optional[str], collection_scope=None) -> dict:
    allow_read = milestone.kind in {"collection", "verification"}
    return {
        "read_instruction": read_instruction,
        "allow_read": bool(read_instruction and allow_read),
        "milestone_id": milestone.id,
        "milestone_kind": milestone.kind,
        "completion_strategy": milestone.completion_strategy,
        "completion_status": milestone.completion_status,
        "collection_scope": collection_scope,
    }


def _is_home_identity(page_identity: str, markers: tuple[str, ...] = ()) -> bool:
    """Derive "system home/launcher" from platform-provided page_identity markers."""
    if not page_identity or not markers:
        return False
    pid = page_identity.lower()
    return any(marker and marker.lower() in pid for marker in markers)
