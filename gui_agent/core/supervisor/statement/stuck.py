"""Stuck detection and picker helpers for the statement supervisor."""

from __future__ import annotations

import re
from typing import Optional

from .schemas import _PlanResult


class StatementStuckMixin:
    """Plan-fixing helpers for picker direction/steps and action sequences."""

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
