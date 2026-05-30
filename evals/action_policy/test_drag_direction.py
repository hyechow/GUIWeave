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
from policy_expr.policies.structured_output import _normalize_drag_direction
from policy_expr.gesture import drag_gesture

passed = 0
failed = 0


def _case(label: str, instruction: str, hint: str, expect_value_dir: str, expect_finger: str) -> None:
    global passed, failed
    a = Action(action_type="drag", x=86, y=445, target_area="picker_left", method="drag",
               direction="up", description=instruction)
    d = ActionDecision(action=a)
    _normalize_drag_direction(d, instruction, hint)
    g = drag_gesture(d.action)
    finger = "up" if g.to_y < g.y else "down"
    ok = d.action.value_direction == expect_value_dir and finger == expect_finger
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    detail = "" if ok else f"  got value_direction={d.action.value_direction!r} finger={finger!r}, expected {expect_value_dir!r}/{expect_finger!r}"
    print(f"  [{tag}] {label:50s}{detail}")


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
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
