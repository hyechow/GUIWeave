"""ProgressMonitor — the single owner of task-execution health (stuck detection).

Stuck is a TRAJECTORY property, not a single-frame one: it only shows up across turns (no real
progress over a window). So it needs a stateful, cross-turn monitor — neither the per-turn LLM
checker (stateless across turns) nor the scattered frame-level heuristics own that. This module
accumulates deterministic FACTS; it does NOT make the semantic judgment ("is this repeat
meaningless?") — that stays with the checker, which reads the rendered trace. It also exposes a
programmatic `repeated()` the supervisor routes to stuck.

This is the task-level memory the frame-level guards miss: a Reset→search→Reset loop changes the
URL and DOM every turn, so each iteration looks like "progress" and the frame detectors get
suppressed — but at the task level it is the same (state, action) recurring.

Two ways to key an action (the `decision`):
  - the planner's NL instruction (legacy Action-Loop Guard) — reworded phrasings collapse via
    `_norm_action`, but a genuinely reworded SAME action can still slip through; and
  - `action_signature(action)` — a reword-proof identity of the EXECUTED action
    (action_type | target-control | text). Run 20260622_171843 re-typed 'Olivia zip jacket' into
    the same box across T3/T9/T12/T13 while the instruction kept changing; the signature collapses
    them, the instruction key did not.
Both flow through the same `note`/`repeated` — the caller picks the key.

state = CANONICAL URL (host + volatile filter/form_key/sort segments stripped), NOT the LLM
page_identity (which drifts ~18 labels for one page). Stripping the filter collapses the
oscillating filtered/unfiltered URLs into one page state, so the loop becomes visible. The dual
signal is state-frontier coverage (`distinct_states` — few distinct states = churn).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from gui_agent.context import ContextBlock

# Volatile URL segments that don't define the page (filter token, csrf form_key, sort/paging…):
# strip so the SAME page collapses to ONE canonical state regardless of applied filter/session.
# [^/]* (not +) so an EMPTY volatile segment collapses too: a cleared filter renders as
# `/filter//internal_reviews`; without this it would NOT match the content-filter URL's state.
_VOLATILE_SEG = re.compile(r"/(filter|form_key|key|uenc|back|isAjax|sort|dir|limit|page)/[^/]*")

# Position bin (px) for the action signature. The DOM snap reports a per-element center that
# jitters a few px between turns (and the WxH in `info` jitters too), so we drop the size and bin
# the snapped center. Coarse enough to absorb the jitter (~6px) yet keep distinct controls apart
# (the Reviews filter boxes sit ~200px apart). Floor-division → deterministic, no rounding edge.
_POS_BIN_PX = 50


def canonical_url(url: str | None) -> str:
    """Canonical page-state id: host-stripped path with volatile segments removed. '' if no url."""
    if not url:
        return ""
    p = re.sub(r"^https?://[^/]+", "", url)
    p = _VOLATILE_SEG.sub("", p)
    p = p.split("?", 1)[0].rstrip("/")
    return p or "/"


def _norm_action(text: str) -> str:
    """Light normalization so superficially-different phrasings of the same action collapse."""
    t = (text or "").lower()
    t = re.sub(r"[\s，。、；;：:！!？?（）()「」『』\[\]【】\"'`’‘“”]+", "", t)
    return t[:80]


def _target_key(action: Any) -> str:
    """A stable identity for the control an action touched: element tag + binned snapped center.

    Prefers the DOM-snapped center (which lands on the element); falls back to the planned x/y.
    Drops the jittery WxH from `info`, keeping only the tag (input/button/a/…)."""
    snap = getattr(action, "snap", None) or {}
    info = snap.get("info") or ""
    tag = info.split(" ", 1)[0].lower() if info else ""
    pt = snap.get("snapped") or [getattr(action, "x", None), getattr(action, "y", None)]
    px = pt[0] if pt else None
    py = pt[1] if pt and len(pt) > 1 else None
    if px is not None and py is not None:
        bucket = f"{int(px // _POS_BIN_PX)},{int(py // _POS_BIN_PX)}"
    else:
        bucket = "-"
    return f"{tag}@{bucket}" if tag else f"@{bucket}"


def action_signature(action: Any) -> str:
    """A reword-proof identity for a concrete action: `type|input@16,8|oliviazipjacket`,
    `tap|button@2,5|`, `scroll|down|@-`. Two turns with the same signature did the same thing,
    however differently the instruction was phrased. Use as the `decision` key in note/repeated."""
    at = getattr(action, "action_type", "") or "?"
    target = _target_key(action)
    if at in ("scroll", "drag"):
        return f"{at}|{getattr(action, 'direction', '') or ''}|{target}"
    return f"{at}|{target}|{_norm_action(getattr(action, 'text', '') or '')}"


@dataclass
class TraceStep:
    index: int
    state: str       # canonical URL (or a page-identity fallback)
    decision: str    # the action key from `state` — an NL instruction OR an action_signature
    interaction_state: str = ""  # optional DOM/form-state fingerprint for browser pages


@dataclass
class ProgressMonitor:
    """The supervisor's accumulated (state, decision) memory for the current run."""

    turns: list[TraceStep] = field(default_factory=list)

    def note(self, index: int, state: str, decision: str, interaction_state: str = "") -> None:
        """Record an acting turn. `state` should already be a canonical_url (or page fallback);
        `decision` is the action key (an instruction, or `action_signature(action)`)."""
        self.turns.append(TraceStep(
            index=index,
            state=state,
            decision=_norm_action(decision),
            interaction_state=interaction_state or "",
        ))

    def repeated(self, state: str, decision: str, interaction_state: str = "") -> Optional[TraceStep]:
        """The earliest prior turn with this same (state, decision), or None. A hit = the agent is
        about to redo a move it already made in a page it already stood on (a loop)."""
        key = _norm_action(decision)
        ix = interaction_state or ""
        for t in self.turns:
            if t.state == state and t.decision == key and t.interaction_state == ix:
                return t
        return None

    def repeat_count(self, state: str, decision: str, interaction_state: str = "") -> int:
        """How many prior turns already executed this (state, decision)."""
        key = _norm_action(decision)
        ix = interaction_state or ""
        return sum(1 for t in self.turns
                   if t.state == state and t.decision == key and t.interaction_state == ix)

    def check_loop(
        self, index: int, state: str, decision: str, interaction_state: str = "",
    ) -> Optional[TraceStep]:
        """Loop guard in one call: if this (state, decision) was already done here, return the prior
        turn (the agent is about to redo a move → a loop) and DON'T re-note it. Otherwise record it
        and return None. No-op when state/decision are empty (e.g. a visual platform with no url)."""
        if not state or not decision:
            return None
        hit = self.repeated(state, decision, interaction_state)
        if hit is not None:
            return hit
        self.note(index, state, decision, interaction_state)
        return None

    def distinct_states(self) -> int:
        return len({t.state for t in self.turns})

    def render(self, recent_n: int = 8) -> str:
        """A compact state→decision trace with repeat/stall markers, for the checker."""
        if not self.turns:
            return ""
        seen: dict[tuple[str, str, str], int] = {}
        prev_state = ""
        lines: list[str] = []
        for t in self.turns:
            tag = ""
            k = (t.state, t.decision, t.interaction_state)
            if k in seen:
                tag = f" ⚠️重复(同 T{seen[k]})"
            elif t.state == prev_state:
                tag = " (同页面)"
            seen.setdefault(k, t.index)
            prev_state = t.state
            ix = f" | 交互:{t.interaction_state[:8]}" if t.interaction_state else ""
            lines.append(f" T{t.index} 状态:{t.state or '?'}{ix} | 决策:{t.decision[:48]}{tag}")
        return "\n".join(lines[-recent_n:])


def state_trace_block(trace: ProgressMonitor, *, recent_n: int = 8) -> Optional[ContextBlock]:
    """Context block carrying the state→decision trace so the checker can judge task progress
    (advancing to new states vs looping in a few)."""
    body = trace.render(recent_n=recent_n)
    if not body.strip():
        return None
    return ContextBlock(
        id="runtime.state_trace",
        budget="high",
        source_type="runtime_state",
        source="state_trace",
        ttl="turn",
        priority=28,
        content=(
            "## 任务进展轨迹（状态→决策，越下越新）\n"
            "状态=页面规范化 URL；标⚠️重复=同一页面上重复了之前做过的同一决策(在打转，不是推进)。"
            "据此判断任务是在推进(不断到达新状态)还是在少数状态里打转。\n"
            + body
        ),
    )
