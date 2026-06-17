"""Stuck detection and picker helpers for the milestone supervisor."""

from __future__ import annotations

import re
from typing import Optional

from gui_agent.core.schemas import Observation
from gui_agent.core.vision.frame_analysis import CHANGE_SSIM_DIST_THR, region_change

from .schemas import _PlanResult, _SingleCheckResult

STUCK_SCREEN_WINDOW = 3
STUCK_SCREEN_SIMILARITY = 0.95
STUCK_SCREEN_FROZEN = 0.99
STUCK_REPEAT_WINDOW = 3
STUCK_REPEAT_WORD_OVERLAP = 0.85
STUCK_VALUE_STALL_WINDOW = 4


class MilestoneStuckMixin:
    def _check_screen_similarity(
        self, observation: Observation, action_center: Optional[tuple[float, float]] = None
    ) -> Optional[_SingleCheckResult]:
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

    def _check_instruction_repetition(
        self,
        history,
        milestone_id: str,
    ) -> Optional[_SingleCheckResult]:
        recent_insts = [
            t.supervisor.instruction
            for t in history[-STUCK_REPEAT_WINDOW:]
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone_id
        ]
        if len(recent_insts) < STUCK_REPEAT_WINDOW:
            return None
        base_words = set(recent_insts[-1].split())
        sims = [
            len(base_words & set(inst.split())) / max(len(base_words), len(set(inst.split())), 1)
            for inst in recent_insts[:-1]
        ]
        if all(s >= STUCK_REPEAT_WORD_OVERLAP for s in sims):
            sim_str = ", ".join(f"{s:.2%}" for s in sims)
            print(f"  [RepStuck] {sim_str} → 指令连续重复")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_REPEAT_WINDOW} 步给出相似指令，当前页面仍未满足验收条件",
                stuck_reason="连续相似指令未达成目标，需要改用当前截图中的其他可见入口或操作顺序",
                issues=["连续操作策略过于相似"],
                summary="操作陷入重复循环",
            )
        return None

    @staticmethod
    def _action_center(action) -> Optional[tuple[float, float]]:
        if action is None:
            return None
        x, y = getattr(action, "x", None), getattr(action, "y", None)
        if x is None or y is None:
            return None
        return (float(x), float(y))

    @staticmethod
    def _is_value_adjust(action) -> bool:
        if action is None:
            return False
        ta = getattr(action, "target_area", "") or ""
        return action.action_type == "scroll" or (
            action.action_type == "drag" and ta.startswith("picker_")
        )

    @staticmethod
    def _extract_progress_value(check: _SingleCheckResult) -> str:
        for ev in check.missing_evidence or []:
            m = re.search(r"当前值\s*[=:：]\s*(.+)", ev.strip())
            if m:
                return re.sub(r"\s+", "", m.group(1))
        text = f"{check.reason or ''}\n{check.summary or ''}"
        time_match = re.search(r"(上午|下午|AM|PM)?\s*0?(\d{1,2})\s*[:：]\s*0?(\d{1,2})", text, flags=re.IGNORECASE)
        if time_match:
            period = (time_match.group(1) or "").upper()
            hour = int(time_match.group(2))
            minute = int(time_match.group(3))
            return f"{period}{hour:02d}:{minute:02d}"
        hour_match = re.search(r"小时(?:列)?(?:中间(?:高亮|选中)?行?|选中值|显示)?(?:为|=|显示为)?['「“]?\s*0?(\d{1,2})", text)
        minute_match = re.search(r"分钟(?:列)?(?:中间(?:高亮|选中)?行?|选中值|显示)?(?:为|=|显示为)?['「“]?\s*0?(\d{1,2})", text)
        if hour_match and minute_match:
            return f"{int(hour_match.group(1)):02d}:{int(minute_match.group(1)):02d}"
        return re.sub(r"\s+", "", check.summary or "")

    def _check_value_stall(
        self, milestone, check: _SingleCheckResult,
    ) -> Optional[_SingleCheckResult]:
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
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留在「{val}」，调整未朝目标推进",
                stuck_reason="连续调值无进展：当前值多轮未变化",
                issues=["监控值多轮未朝目标推进（疑似方向错/步长不足/非法值回弹）"],
                summary=check.summary,
            )
        return None

    @staticmethod
    def _picker_drag_steps(plan: _PlanResult) -> Optional[int]:
        if not getattr(plan, "drag_column", None):
            return None
        cur = getattr(plan, "drag_current_value", None)
        tgt = getattr(plan, "drag_target_value", None)
        if cur is None or tgt is None:
            return None
        if tgt != cur:
            column = (getattr(plan, "drag_column", None) or "").strip().lower()
            if column == "minute":
                forward = (tgt - cur) % 60
                backward = (cur - tgt) % 60
                plan.direction = "increase" if forward <= backward else "decrease"
                return min(forward, backward)
            if column == "hour":
                forward = (tgt - cur) % 12
                backward = (cur - tgt) % 12
                plan.direction = "increase" if forward <= backward else "decrease"
                return min(forward, backward)
            plan.direction = "increase" if tgt > cur else "decrease"
        return abs(tgt - cur)

    @staticmethod
    def _fix_picker_direction(plan: _PlanResult) -> None:
        col = getattr(plan, "drag_column", None) or ""
        col_suffix = {"year": "年", "month": "月", "day": "日"}.get(col, "")
        if not col_suffix:
            return
        higher_suffixes = {"day": ["月", "年"], "month": ["年"], "year": []}.get(col, [])
        for hs in higher_suffixes:
            hvals = re.findall(rf"(\d+){hs}", plan.instruction)
            if len(hvals) >= 2 and len(set(hvals[:2])) > 1:
                print(f"  [Planner] direction fix 跳过：跨{hs}边界（{hvals[0]}{hs}→{hvals[1]}{hs}），本列数字比较无效")
                return
        nums = re.findall(rf"(\d+){col_suffix}", plan.instruction)
        if len(nums) < 2:
            return
        cur, tgt = int(nums[0]), int(nums[1])
        if tgt == cur:
            return
        correct = "increase" if tgt > cur else "decrease"
        if plan.direction != correct:
            print(f"  [Planner] direction fix: {plan.direction} → {correct} ({cur}→{tgt})")
            plan.direction = correct
            if correct == "increase":
                for old, new in [("向下拖动", "向上拖动"), ("下拉", "上拉"), ("往下", "往上")]:
                    plan.instruction = plan.instruction.replace(old, new)
            else:
                for old, new in [("向上拖动", "向下拖动"), ("上拉", "下拉"), ("往上", "往下")]:
                    plan.instruction = plan.instruction.replace(old, new)

    @staticmethod
    def _is_sequence(instruction: str) -> bool:
        text = instruction.strip()
        markers = ("操作序列", "步骤", "\n1.", "\n2.", "1.", "2.", "；2", ";2")
        return any(m in text for m in markers)

    def _is_repeated_instruction(
        self, instruction: str, milestone_id: str, history,
    ) -> bool:
        from difflib import SequenceMatcher
        _strip = lambda s: re.sub(r"[，。、；：""''《》\s（）\(\)]", "", s.strip())
        _scroll_words = ("滚动", "滑动", "拖动", "拖拽", "scroll", "drag")
        n_new = _strip(instruction)

        stuck_tried: set[str] = set()
        all_tried: list[str] = []
        for idx, t in enumerate(history):
            sv = t.supervisor
            if not sv or not sv.instruction:
                continue
            if sv.milestone_id == milestone_id:
                all_tried.append(sv.instruction)
            next_sv = history[idx + 1].supervisor if idx + 1 < len(history) else None
            if (
                next_sv
                and ("卡住" in (next_sv.summary or "") or "重试" in (next_sv.summary or ""))
            ):
                stuck_tried.add(sv.instruction)

        def _similar(new: str, old: str) -> bool:
            n_old = _strip(old)
            return bool(n_new and n_old) and SequenceMatcher(None, new, n_old).ratio() >= 0.6

        for old in stuck_tried:
            if _similar(n_new, old):
                return True

        if any(w in instruction for w in _scroll_words):
            return False
        similar_count = sum(1 for old in all_tried if _similar(n_new, old))
        return similar_count >= 2
