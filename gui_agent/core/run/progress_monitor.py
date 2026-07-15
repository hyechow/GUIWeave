"""ProgressMonitor — the single owner of task-execution health (stuck detection).

Stuck is a TRAJECTORY property, not a single-frame one: it only shows up across turns (no real
progress over a window). So it needs a stateful, cross-turn monitor — neither the per-turn LLM
checker (stateless across turns) nor the scattered frame-level heuristics own that. This module
accumulates deterministic FACTS; it does NOT advance, retry, or replan execution. Its detectors
emit typed progress assessments for the milestone policy's single recovery path. Completion and
persistence evidence remain independent of trajectory health.

This is the task-level memory the frame-level guards miss: a Reset→search→Reset loop changes the
URL and DOM every turn, so each iteration looks like "progress" and the frame detectors get
suppressed — but at the task level it is the same (state, action) recurring.

Two ways to key an action (the `decision`):
  - the planner's NL instruction (legacy Action-Loop Guard) — reworded phrasings collapse via
    `normalize_action_text`, but a genuinely reworded SAME action can still slip through; and
  - `action_signature(action)` — a reword-proof identity of the EXECUTED action
    (action_type | target-control | text). Run 20260622_171843 re-typed the same long search value
    into the same box across T3/T9/T12/T13 while the instruction kept changing; the signature collapses
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
from gui_agent.core.run.action_signals import normalize_action_text
from gui_agent.core.run.instruction_similarity import instructions_are_repeated
from gui_agent.core.schemas import Observation
from gui_agent.core.vision.frame_analysis import CHANGE_SSIM_DIST_THR, region_change

# Volatile URL segments that don't define the page (filter token, csrf form_key, sort/paging…):
# strip so the SAME page collapses to ONE canonical state regardless of applied filter/session.
# [^/]* (not +) so an EMPTY volatile segment collapses too: a cleared filter renders as
# `/filter//internal_reviews`; without this it would NOT match the content-filter URL's state.
_VOLATILE_SEG = re.compile(r"/(filter|form_key|key|uenc|back|isAjax|sort|dir|limit|page)/[^/]*")

# Frame/instruction/value stuck-detector thresholds (moved here from stuck.py — the monitor owns
# the deterministic stuck facts).
STUCK_SCREEN_WINDOW = 3
STUCK_SCREEN_SIMILARITY = 0.95
STUCK_SCREEN_FROZEN = 0.99
STUCK_REPEAT_WINDOW = 3
STUCK_REPEAT_WORD_OVERLAP = 0.85
STUCK_VALUE_STALL_WINDOW = 4


@dataclass(frozen=True)
class ProgressAssessment:
    """A trajectory fact emitted by the monitor, without control-flow authority."""

    status: Literal["advancing", "stalled", "exhausted"] = "advancing"
    reason: str = ""
    source_type: str = "runtime.progress"
    frozen: bool = False


def canonical_url(url: str | None) -> str:
    """Canonical page-state id: host-stripped path with volatile segments removed. '' if no url."""
    if not url:
        return ""
    p = re.sub(r"^https?://[^/]+", "", url)
    p = _VOLATILE_SEG.sub("", p)
    p = p.split("?", 1)[0].rstrip("/")
    return p or "/"


@dataclass
class TraceStep:
    index: int
    state: str       # canonical URL (or a page-identity fallback)
    decision: str    # the action key from `state` — an NL instruction OR an action_signature
    interaction_state: str = ""  # optional DOM form-value fingerprint for browser pages
    scope: str = ""  # execution bucket: milestone:<id> or row:<identity>


@dataclass
class ProgressMonitor:
    """The supervisor's accumulated (state, decision) memory for the current run."""

    turns: list[TraceStep] = field(default_factory=list)
    # Kind-1 "did the last action have an effect" facts (set by observe_effect each turn).
    # URL/form-value deltas are GROUND TRUTH that the previous action changed something — used to
    # suppress pixel-based false positives (false no_effect, false sim/rep-stuck on a form fill).
    url_changed: bool = False
    dom_changed: bool = False
    _last_url: Optional[str] = None        # raw url (not canonical) — exact-delta comparison
    _last_dom_state: Optional[str] = None  # form values / checked-state fingerprint
    # Per-milestone sliding window of the checker's read "current value" — the value-stall detector.
    _progress_values: list[str] = field(default_factory=list)
    # Recent (frame, action-center) pairs for the screen-similarity detector. Cleared on milestone
    # transitions (the touched region differs); a transient buffer, not persisted.
    _recent_screenshots: list = field(default_factory=list)

    def observe_effect(self, url: Optional[str], dom_state: Optional[str]) -> None:
        """Update `url_changed` / `dom_changed` from this turn's observation: a changed url means
        the previous action navigated (a definite effect); a changed interaction fingerprint means
        it changed a form value. None (visual platforms, no url/dom) → stays False (no signal)."""
        self.url_changed = bool(url and self._last_url is not None and url != self._last_url)
        if url is not None:
            self._last_url = url
        self.dom_changed = bool(dom_state and self._last_dom_state is not None and dom_state != self._last_dom_state)
        if dom_state is not None:
            self._last_dom_state = dom_state

    def note(
        self,
        index: int,
        state: str,
        decision: str,
        interaction_state: str = "",
        scope: str = "",
    ) -> None:
        """Record an acting turn. `state` should already be a canonical_url (or page fallback);
        `decision` is the action key (an instruction, or `action_signature(action)`)."""
        self.turns.append(TraceStep(
            index=index,
            state=state,
            decision=normalize_action_text(decision),
            interaction_state=interaction_state or "",
            scope=scope or "",
        ))

    def repeated(
        self,
        state: str,
        decision: str,
        interaction_state: str = "",
        scope: str = "",
    ) -> Optional[TraceStep]:
        """The earliest prior turn with this same (state, decision), or None. A hit = the agent is
        about to redo a move it already made in a page it already stood on (a loop)."""
        key = normalize_action_text(decision)
        ix = interaction_state or ""
        for t in self.turns:
            if scope and t.scope != scope:
                continue
            if t.state == state and t.decision == key and t.interaction_state == ix:
                return t
        return None

    def check_loop(
        self, index: int, state: str, decision: str, interaction_state: str = "", scope: str = "",
    ) -> Optional[TraceStep]:
        """Loop guard in one call: if this (state, decision) was already done here, return the prior
        turn (the agent is about to redo a move → a loop) and DON'T re-note it. Otherwise record it
        and return None. No-op when state/decision are empty (e.g. a visual platform with no url)."""
        if not state or not decision:
            return None
        hit = self.repeated(state, decision, interaction_state, scope)
        if hit is not None:
            return hit
        self.note(index, state, decision, interaction_state, scope)
        return None

    # ── deterministic stuck detectors (moved from stuck.py) ────────────────
    def clear_screenshots(self) -> None:
        """Reset the screen-similarity buffer (called on milestone transitions)."""
        self._recent_screenshots.clear()

    def reset_for_retry(self) -> None:
        """Clear transient observation windows while retaining the action trace.

        Return-contract tighten is the same statement invocation, so prior actions remain useful
        loop evidence.  Frame similarity, value-stall windows, and the last computed delta flags
        belong to the superseded completion attempt and must start fresh.
        """
        self._recent_screenshots.clear()
        self._progress_values.clear()
        self.url_changed = False
        self.dom_changed = False

    @staticmethod
    def action_center(action) -> Optional[tuple[float, float]]:
        """The x/y the last action touched — the region the screen-similarity detector inspects."""
        if action is None:
            return None
        x, y = getattr(action, "x", None), getattr(action, "y", None)
        if x is None or y is None:
            return None
        return (float(x), float(y))

    def check_screen_similarity(
        self, observation: Observation, action_center: Optional[tuple[float, float]] = None,
    ):
        """Report stalled progress when recent frames freeze or oscillate; otherwise return None."""
        self._recent_screenshots.append((observation.png_bytes, action_center))
        if len(self._recent_screenshots) > STUCK_SCREEN_WINDOW:
            self._recent_screenshots.pop(0)
        if len(self._recent_screenshots) < STUCK_SCREEN_WINDOW:
            return None

        frames = self._recent_screenshots
        gsims: list[float] = []
        locs: list[Optional[float]] = []
        for i in range(1, len(frames)):
            gs, lc = region_change(frames[i - 1][0], frames[i][0], frames[i][1])
            gsims.append(gs)
            locs.append(lc)

        def _no_change(gs: float, lc: Optional[float]) -> bool:
            return gs >= STUCK_SCREEN_SIMILARITY and (lc is None or lc <= CHANGE_SSIM_DIST_THR)

        if all(_no_change(gs, lc) for gs, lc in zip(gsims, locs)):
            gstr = ", ".join(f"{g:.2%}" for g in gsims)
            lstr = ", ".join("∅" if l is None else f"{l:.3f}" for l in locs)
            frozen = max(gsims) >= STUCK_SCREEN_FROZEN
            tag = "屏幕冻结（局部+全局均无变化）" if frozen else "连续无变化（局部+全局）"
            print(f"  [SimStuck] 全局[{gstr}] 局部[{lstr}] → {tag}")
            return ProgressAssessment(
                status="stalled",
                reason=f"连续 {STUCK_SCREEN_WINDOW} 帧没有看到与目标相关的页面变化",
                source_type="runtime.screen_progress",
                frozen=frozen,
            )
        sim_2back, _ = region_change(frames[-1][0], frames[-3][0])
        sim_adj, _ = region_change(frames[-1][0], frames[-2][0])
        if sim_2back >= STUCK_SCREEN_SIMILARITY and sim_adj < STUCK_SCREEN_SIMILARITY:
            print(f"  [SimStuck] 2back={sim_2back:.2%}, adj={sim_adj:.2%} → AB 循环")
            return ProgressAssessment(
                status="stalled",
                reason="页面在两个可见状态之间来回切换，验收条件仍未出现",
                source_type="runtime.screen_progress",
            )
        return None

    def check_instruction_repetition(self, history, milestone_id: str):
        """Report stalled progress when recent milestone instructions are near-identical."""
        recent = [
            t.supervisor.instruction
            for t in history[-STUCK_REPEAT_WINDOW:]
            if t.supervisor and t.supervisor.instruction and t.supervisor.milestone_id == milestone_id
        ]
        if len(recent) < STUCK_REPEAT_WINDOW:
            return None
        repeated = [
            instructions_are_repeated(recent[-1], inst, threshold=STUCK_REPEAT_WORD_OVERLAP)
            for inst in recent[:-1]
        ]
        if all(repeated):
            print("  [RepStuck] 指令目标一致且文本连续相似 → 指令连续重复")
            return ProgressAssessment(
                status="stalled",
                reason=f"连续 {STUCK_REPEAT_WINDOW} 步给出相似指令，当前页面仍未满足验收条件",
                source_type="runtime.instruction_progress",
            )
        return None

    @staticmethod
    def is_value_adjust(action) -> bool:
        """True for continuous value-adjust actions (scroll / picker drag) — value-stall applies."""
        if action is None:
            return False
        ta = getattr(action, "target_area", "") or ""
        return action.action_type == "scroll" or (
            action.action_type == "drag" and ta.startswith("picker_")
        )

    @staticmethod
    def _extract_progress_value(check) -> str:
        """The 'current value' the checker read this turn (for value-stall), from missing_evidence
        '当前值=…' or a time/hour-minute pattern in reason/summary."""
        for ev in check.missing_evidence or []:
            m = re.search(r"当前值\s*[=:：]\s*(.+)", ev.strip())
            if m:
                return re.sub(r"\s+", "", m.group(1))
        text = f"{check.reason or ''}\n{check.summary or ''}"
        tm = re.search(r"(上午|下午|AM|PM)?\s*0?(\d{1,2})\s*[:：]\s*0?(\d{1,2})", text, flags=re.IGNORECASE)
        if tm:
            return f"{(tm.group(1) or '').upper()}{int(tm.group(2)):02d}:{int(tm.group(3)):02d}"
        hm = re.search(r"小时(?:列)?(?:中间(?:高亮|选中)?行?|选中值|显示)?(?:为|=|显示为)?['「“]?\s*0?(\d{1,2})", text)
        mm = re.search(r"分钟(?:列)?(?:中间(?:高亮|选中)?行?|选中值|显示)?(?:为|=|显示为)?['「“]?\s*0?(\d{1,2})", text)
        if hm and mm:
            return f"{int(hm.group(1)):02d}:{int(mm.group(1)):02d}"
        return re.sub(r"\s+", "", check.summary or "")

    def check_value_stall(self, check):
        """Report stalled progress when a continuously adjusted value remains unchanged."""
        val = self._extract_progress_value(check)
        window = self._progress_values
        window.append(val)
        if len(window) > STUCK_VALUE_STALL_WINDOW:
            window.pop(0)
        if len(window) < STUCK_VALUE_STALL_WINDOW or not val:
            return None
        if len(set(window)) == 1:
            print(f"  [ValueStall] 连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留「{val}」，未朝目标推进")
            window.clear()
            return ProgressAssessment(
                status="stalled",
                reason=f"连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留在「{val}」，调整未朝目标推进",
                source_type="runtime.value_progress",
            )
        return None

    def distinct_states(self) -> int:
        return len({t.state for t in self.turns})

    def render(self, recent_n: int = 8, scope: str = "") -> str:
        """A compact state→decision trace with repeat/stall markers, for the checker."""
        turns = [
            t for t in self.turns
            if not scope or t.scope == scope
        ]
        if not turns:
            return ""
        seen: dict[tuple[str, str, str], int] = {}
        prev_state = ""
        lines: list[str] = []
        for t in turns:
            tag = ""
            k = (t.state, t.decision, t.interaction_state)
            if k in seen:
                tag = f" ⚠️重复(同 T{seen[k]})"
            elif t.state == prev_state:
                tag = " (同页面)"
            seen.setdefault(k, t.index)
            prev_state = t.state
            ix = f" | 交互:{t.interaction_state[:8]}" if t.interaction_state else ""
            scope_txt = f" | scope:{t.scope}" if t.scope and not scope else ""
            lines.append(f" T{t.index} 状态:{t.state or '?'}{ix}{scope_txt} | 决策:{t.decision[:48]}{tag}")
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
