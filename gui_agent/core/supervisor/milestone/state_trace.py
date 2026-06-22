"""Action-Loop Guard: a structured task-state memory that catches action-level loops.

The frame-level stuck detectors (screen similarity, instruction repetition in stuck.py) are DEFEATED
by oscillation: a Reset→search→Reset loop changes the URL and DOM every turn, so each iteration looks
like "progress" and the detectors get suppressed ([URLChanged]/[DOMChanged] 抑制). But at the TASK
level it is the same failed (state, action) repeating. This module is the missing task-level memory:
it records each acting turn as a {state, decision} and flags when a (state, action) RECURS.

Validated design (see scripts/repeat_mistake_113.py + the stuck-diagnosis-two-mechanisms memory):
  - state = CANONICAL URL (host + volatile filter/form_key/sort segments stripped) — NOT the LLM
    page_identity (which drifts ~18 labels for one page). Stripping the filter collapses the
    oscillating "filtered"/"unfiltered" URLs into one page state, so the loop becomes visible.
  - decision = the action / instruction.
  - a REPEAT = a (state, action) already seen earlier in the trace → the agent is re-doing a move it
    already made in a page it already stood on, instead of advancing the state frontier.

It serves two consumers: (1) a rendered trace fed to the checker so it can judge progress; (2) a
programmatic `repeated()` check the supervisor routes to stuck (the active catch the oscillation
defeats). State-frontier coverage (distinct states) is the dual signal — few distinct states = churn."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from gui_agent.context import ContextBlock

# Volatile URL segments that don't define the page (filter token, csrf form_key, sort/paging…):
# strip so the SAME page collapses to ONE canonical state regardless of applied filter/session.
# [^/]* (not +) so an EMPTY volatile segment collapses too: a cleared filter renders as
# `/filter//internal_reviews`; without this it would NOT match the content-filter URL's state.
_VOLATILE_SEG = re.compile(r"/(filter|form_key|key|uenc|back|isAjax|sort|dir|limit|page)/[^/]*")


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


@dataclass
class StateTurn:
    index: int
    state: str       # canonical URL (or a page-identity fallback)
    decision: str    # the action / instruction taken from `state`


@dataclass
class StateTrace:
    """The supervisor's accumulated (state, decision) memory for the current run."""

    turns: list[StateTurn] = field(default_factory=list)

    def note(self, index: int, state: str, decision: str) -> None:
        """Record an acting turn. `state` should already be a canonical_url (or page fallback)."""
        self.turns.append(StateTurn(index=index, state=state, decision=_norm_action(decision)))

    def repeated(self, state: str, decision: str) -> Optional[StateTurn]:
        """The earliest prior turn with this same (state, action), or None. A hit = the agent is
        about to redo a move it already made in a page it already stood on (a loop)."""
        key = _norm_action(decision)
        for t in self.turns:
            if t.state == state and t.decision == key:
                return t
        return None

    def distinct_states(self) -> int:
        return len({t.state for t in self.turns})

    def render(self, recent_n: int = 8) -> str:
        """A compact state→decision trace with repeat/stall markers, for the checker."""
        if not self.turns:
            return ""
        seen: dict[tuple[str, str], int] = {}
        prev_state = ""
        lines: list[str] = []
        for t in self.turns:
            tag = ""
            k = (t.state, t.decision)
            if k in seen:
                tag = f" ⚠️重复(同 T{seen[k]})"
            elif t.state == prev_state:
                tag = " (同页面)"
            seen.setdefault(k, t.index)
            prev_state = t.state
            lines.append(f" T{t.index} 状态:{t.state or '?'} | 决策:{t.decision[:48]}{tag}")
        return "\n".join(lines[-recent_n:])


def state_trace_block(trace: StateTrace, *, recent_n: int = 8) -> Optional[ContextBlock]:
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
