"""Unit test for MilestoneSupervisorPolicy._fix_picker_direction (deterministic, no LLM).

跨边界回归：拖某列时若指令里更高位列取值不同（如 3月31→4月7，月份 3≠4），单看本列数字
（31 vs 7）会得出相反方向。修复后应跳过翻转，保留 planner 的方向。源 case：20260530_112049
turn7-9 因 _fix_picker_direction 把正确的 increase 误翻成 decrease，导致日列徒劳拖动。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gui_agent.core.supervisor.milestone import _PlanResult
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

passed = 0
failed = 0


def _case(label: str, instruction: str, direction: str, drag_column: str, expect_direction: str) -> None:
    global passed, failed
    plan = _PlanResult(instruction=instruction, summary="", direction=direction, drag_column=drag_column)
    MilestoneSupervisorPolicy._fix_picker_direction(plan)
    ok = plan.direction == expect_direction
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    detail = "" if ok else f"  expected direction={expect_direction!r}, got {plan.direction!r}"
    print(f"  [{tag}] {label:55s}{detail}")


def main() -> int:
    print("── _fix_picker_direction Unit ──")
    # 跨月：拖日列但月份不同(3≠4)→跳过翻转，保留 planner 的 increase（核心回归）
    _case("跨月-拖日列-planner判increase-应保留increase",
          "向上拖动右侧日期列，将结束日期从2026年3月31日调整为2026年4月7日",
          "increase", "day", "increase")
    # 跨月：即便 planner 误判 decrease，也不翻成 increase（不靠本列数字救场，交给 A 选月份列）
    _case("跨月-拖日列-planner判decrease-跳过不翻",
          "向下拖动右侧日期列，将结束日期从2026年3月31日调整为2026年4月7日",
          "decrease", "day", "decrease")
    # 同月：拖日列方向反了→应翻转修正（10→7，decrease）
    _case("同月-拖日列-10日→7日-应翻成decrease",
          "向上拖动日期列，将日期从2026年4月10日调整为2026年4月7日",
          "increase", "day", "decrease")
    # 拖月份列：5月→1月，方向反了→应翻成 decrease（高位无更高列差异，正常翻转）
    _case("拖月份列-5月→1月-应翻成decrease",
          "向上拖动月份列，将开始日期从2026年5月1日调整为2026年1月20日",
          "increase", "month", "decrease")
    # 拖日列同年同月-1日→20日-应翻成increase
    _case("同年同月-拖日列-1日→20日-应翻成increase",
          "向下拖动日期列，将开始日期从2026年1月1日调整为2026年1月20日",
          "decrease", "day", "increase")
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
