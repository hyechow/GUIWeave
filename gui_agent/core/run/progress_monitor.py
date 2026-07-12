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
from gui_agent.core.run.instruction_similarity import instructions_are_repeated
from gui_agent.core.schemas import Observation
from gui_agent.core.vision.frame_analysis import CHANGE_SSIM_DIST_THR, region_change

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

# Frame/instruction/value stuck-detector thresholds (moved here from stuck.py — the monitor owns
# the deterministic stuck facts).
STUCK_SCREEN_WINDOW = 3
STUCK_SCREEN_SIMILARITY = 0.95
STUCK_SCREEN_FROZEN = 0.99
STUCK_REPEAT_WINDOW = 3
STUCK_REPEAT_WORD_OVERLAP = 0.85
STUCK_VALUE_STALL_WINDOW = 4


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
    _progress_values: dict = field(default_factory=dict)
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
            decision=_norm_action(decision),
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
        key = _norm_action(decision)
        ix = interaction_state or ""
        for t in self.turns:
            if scope and t.scope != scope:
                continue
            if t.state == state and t.decision == key and t.interaction_state == ix:
                return t
        return None

    def repeat_count(
        self,
        state: str,
        decision: str,
        interaction_state: str = "",
        scope: str = "",
    ) -> int:
        """How many prior turns already executed this (state, decision)."""
        key = _norm_action(decision)
        ix = interaction_state or ""
        return sum(1 for t in self.turns
                   if t.state == state and t.decision == key and t.interaction_state == ix
                   and not (scope and t.scope != scope))

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

    @staticmethod
    def check_action_repetition(
        history,
        milestone_id,
        *,
        execution_scope: str = "",
        min_repeats: int = 2,
    ) -> Optional[str]:
        """URL-independent type-repeat catch: within THIS milestone, has the most-recently executed
        `type` action already put the SAME concrete value into the SAME box ≥ min_repeats times?
        Returns that signature, else None.

        Why not the url-keyed check_loop: a filter/search/reset cycle rewrites the url's path shape
        (`/index/` → `/index/filter//internal_reviews/…`), so canonical_url is NOT stable across the
        first-search boundary — the first re-type lands under a different state and never matches
        (regression 20260622_205544: the same long search value was typed at T3/T6/T10, the guard
        never fired).
        The signature alone (type|box|value) IS the identity; scanning the milestone's own executed
        type actions needs no url. Scoped to `type`: re-putting the same value into the same box is
        always redundant, whereas a re-click of Search/Reset can be legitimate (instruction guard owns
        those)."""
        sigs: list[str] = []
        for t in history:
            sv = getattr(t, "supervisor", None)
            ad = getattr(t, "action_decision", None)
            same_milestone = sv and getattr(sv, "milestone_id", None) == milestone_id
            same_scope = (
                not execution_scope
                or not sv
                or not getattr(sv, "execution_scope", "")
                or getattr(sv, "execution_scope", "") == execution_scope
            )
            if not (getattr(t, "executed", False) and same_milestone and same_scope and ad):
                continue
            action = getattr(ad, "action", None)
            if action is not None and getattr(action, "action_type", "") == "type":
                sigs.append(action_signature(action))
        if not sigs:
            return None
        last = sigs[-1]
        return last if sigs.count(last) >= min_repeats else None

    # ── deterministic stuck detectors (moved from stuck.py) ────────────────
    def clear_screenshots(self) -> None:
        """Reset the screen-similarity buffer (called on milestone transitions)."""
        self._recent_screenshots.clear()

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
        """Stuck if the last STUCK_SCREEN_WINDOW frames show no change (frozen) in both the touched
        region and globally, or if the page is oscillating A↔B. Returns a stuck check or None."""
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

        from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
        if all(_no_change(gs, lc) for gs, lc in zip(gsims, locs)):
            gstr = ", ".join(f"{g:.2%}" for g in gsims)
            lstr = ", ".join("∅" if l is None else f"{l:.3f}" for l in locs)
            frozen = max(gsims) >= STUCK_SCREEN_FROZEN
            tag = "屏幕冻结（局部+全局均无变化）" if frozen else "连续无变化（局部+全局）"
            print(f"  [SimStuck] 全局[{gstr}] 局部[{lstr}] → {tag}")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_SCREEN_WINDOW} 帧没有看到与目标相关的页面变化",
                stuck_reason="上一步操作后页面没有出现新内容或目标状态，需要尝试其他可见入口",
                issues=["连续多帧未看到页面状态推进"],
                summary="屏幕连续无变化",
                frozen=frozen,
            )
        sim_2back, _ = region_change(frames[-1][0], frames[-3][0])
        sim_adj, _ = region_change(frames[-1][0], frames[-2][0])
        if sim_2back >= STUCK_SCREEN_SIMILARITY and sim_adj < STUCK_SCREEN_SIMILARITY:
            print(f"  [SimStuck] 2back={sim_2back:.2%}, adj={sim_adj:.2%} → AB 循环")
            return _SingleCheckResult(
                status="stuck",
                reason="页面在两个可见状态之间来回切换，验收条件仍未出现",
                stuck_reason="页面在两个状态之间反复切换，需要换路径或先关闭当前弹窗/面板",
                issues=["页面状态来回切换"],
                summary="页面在两个状态之间反复切换",
            )
        return None

    def check_instruction_repetition(self, history, milestone_id: str):
        """Stuck if the last STUCK_REPEAT_WINDOW instructions for this milestone are near-identical
        (word overlap ≥ threshold) — the planner is circling. Returns a stuck check or None."""
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
            from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_REPEAT_WINDOW} 步给出相似指令，当前页面仍未满足验收条件",
                stuck_reason="连续相似指令未达成目标，需要改用当前截图中的其他可见入口或操作顺序",
                issues=["连续操作策略过于相似"],
                summary="操作陷入重复循环",
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

    def check_value_stall(self, milestone, check):
        """Stuck if the checker's read value stays identical for STUCK_VALUE_STALL_WINDOW turns —
        a continuous adjustment that isn't approaching the target (wrong direction / too-small step)."""
        val = self._extract_progress_value(check)
        window = self._progress_values.setdefault(milestone.id, [])
        window.append(val)
        if len(window) > STUCK_VALUE_STALL_WINDOW:
            window.pop(0)
        if len(window) < STUCK_VALUE_STALL_WINDOW or not val:
            return None
        if len(set(window)) == 1:
            print(f"  [ValueStall] 连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留「{val}」，未朝目标推进")
            window.clear()
            from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留在「{val}」，调整未朝目标推进",
                stuck_reason="连续调值无进展：当前值多轮未变化",
                issues=["监控值多轮未朝目标推进（疑似方向错/步长不足/非法值回弹）"],
                summary=check.summary,
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
