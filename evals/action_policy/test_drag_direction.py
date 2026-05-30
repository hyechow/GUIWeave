"""Unit test: picker drag direction chain (deterministic, no LLM).

回归：_normalize_drag_direction 曾因缩进错误把 direction_hint 处理放到 return 之后成死代码，
导致 planner 的 increase/decrease 完全没生效 → 落到关键词分支把「向上」当 finger 方向 → 被
gesture 反向解读（向上=数值变小）→ picker 数值朝反方向无限发散（20260530_120154 年份 2026→2016）。
本测试锁定：direction_hint=increase → 手指上(露出更大值)；decrease → 手指下(露出更小值)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from policy_expr.schemas import Action, ActionDecision
from policy_expr.policies.structured_output import _normalize_drag_direction, _force_picker_column
from policy_expr.gesture import drag_gesture

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {label:55s}{detail}")


def _case(label: str, instruction: str, hint: str, expect_value_dir: str, expect_finger: str) -> None:
    a = Action(action_type="drag", x=86, y=445, target_area="picker_left", method="drag",
               direction="up", description=instruction)
    d = ActionDecision(action=a)
    _normalize_drag_direction(d, instruction, hint)
    g = drag_gesture(d.action)
    finger = "up" if g.to_y < g.y else "down"
    ok = d.action.value_direction == expect_value_dir and finger == expect_finger
    detail = "" if ok else f"  got value_direction={d.action.value_direction!r} finger={finger!r}, expected {expect_value_dir!r}/{expect_finger!r}"
    _report(label, ok, detail)


def _col_case(label: str, drag_column: str, llm_area: str, llm_x, expect_area: str) -> None:
    a = Action(action_type="drag", x=llm_x, y=445, target_area=llm_area, method="drag",
               value_direction="decrease", description="拖动")
    d = ActionDecision(action=a)
    _force_picker_column(d, drag_column)
    ok = d.action.target_area == expect_area and d.action.x is None
    detail = "" if ok else f"  got target_area={d.action.target_area!r} x={d.action.x!r}, expected {expect_area!r}/None"
    _report(label, ok, detail)


def main() -> int:
    print("── picker drag direction Unit ──")
    # 核心回归：increase → 手指上（露出更大值），decrease → 手指下（露出更小值）
    _case("hint=increase（向上拖动年份）→value=increase,手指up",
          "向上拖动年份列以调整年份值", "increase", "increase", "up")
    _case("hint=decrease（向下拖动月份）→value=decrease,手指down",
          "向下拖动月份列从5月到3月", "decrease", "decrease", "down")
    # planner 跨月校正后给 increase（3月31→4月7 拖月份列）
    _case("hint=increase（跨月拖月份）→value=increase,手指up",
          "向上拖动月份列从3月到4月", "increase", "increase", "up")

    # _force_picker_column：drag_column 硬覆盖 LLM 从文本选错的列（回归 20260530_121615 turn1）
    _col_case("drag_column=month 覆盖 LLM 误选的年份列(picker_left)→picker_center",
              "month", "picker_left", 270, "picker_center")
    _col_case("drag_column=day 覆盖 LLM 误选的月份列→picker_right",
              "day", "picker_center", None, "picker_right")
    _col_case("drag_column=year 且 LLM 已对(picker_left)→保持 picker_left",
              "year", "picker_left", None, "picker_left")
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
