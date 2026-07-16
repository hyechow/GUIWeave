"""Normalize picker action metadata without owning Statement flow."""

from __future__ import annotations

import re
from typing import Optional

from .schemas import _ActionDraft


class StatementActionNormalizationMixin:
    """Keep numeric picker direction and distance internally consistent."""

    @staticmethod
    def _canonical_instruction(plan: _ActionDraft) -> str:
        """Render the one typed action without interpreting free-form prose.

        ``action_family`` plus its structured target/value fields own the primitive. The model's
        prose remains useful only for action families whose payload is not structurally complete.
        """
        target = plan.target_control.strip()
        value = plan.target_value.strip()
        if plan.action_family == "input" and target and value:
            return f"Input {value!r} into the visible {target!r} control."
        if plan.action_family == "select" and target and value:
            return f"Select {value!r} in the visible {target!r} control."
        if plan.action_family == "activate" and target:
            return f"Activate the visible {target!r} control."
        if plan.action_family == "navigate" and target:
            return f"Open the visible {target!r} entry."
        return plan.instruction

    @staticmethod
    def _picker_drag_steps(plan: _ActionDraft) -> Optional[int]:
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
    def _fix_picker_direction(plan: _ActionDraft) -> None:
        col = getattr(plan, "drag_column", None) or ""
        col_suffix = {"year": "年", "month": "月", "day": "日"}.get(col, "")
        if not col_suffix:
            return
        higher_suffixes = {"day": ["月", "年"], "month": ["年"], "year": []}.get(col, [])
        for suffix in higher_suffixes:
            values = re.findall(rf"(\d+){suffix}", plan.instruction)
            if len(values) >= 2 and len(set(values[:2])) > 1:
                print(
                    "  [TransitionAction] direction fix 跳过："
                    f"跨{suffix}边界（{values[0]}{suffix}→{values[1]}{suffix}），本列数字比较无效"
                )
                return
        values = re.findall(rf"(\d+){col_suffix}", plan.instruction)
        if len(values) < 2:
            return
        cur, tgt = int(values[0]), int(values[1])
        if tgt == cur:
            return
        correct = "increase" if tgt > cur else "decrease"
        if plan.direction == correct:
            return
        print(f"  [TransitionAction] direction fix: {plan.direction} → {correct} ({cur}→{tgt})")
        plan.direction = correct
        replacements = (
            [("向下拖动", "向上拖动"), ("下拉", "上拉"), ("往下", "往上")]
            if correct == "increase"
            else [("向上拖动", "向下拖动"), ("上拉", "下拉"), ("往上", "往下")]
        )
        for old, new in replacements:
            plan.instruction = plan.instruction.replace(old, new)
