"""Repeat detection eval: validates _is_repeated_instruction logic.

Tests two detection modes:
  1. Stuck-causing: similar instruction led to stuck → block immediately
  2. Persistent failure: same intent tried >= 2 times without progress → block

No LLM calls, no screenshots — pure logic tests.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from gui_agent.core.schemas import PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone import MilestoneSupervisorPolicy

MID = "m1"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:55s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _turn(instruction: str, summary: str = "", milestone_id: str = MID) -> PolicyTurn:
    return PolicyTurn(
        index=0,
        observation_source="eval",
        supervisor=SupervisorStep(
            should_act=True,
            instruction=instruction,
            stop=False,
            goal_completed=False,
            summary=summary,
            milestone_id=milestone_id,
        ),
        executed=True,
    )


def _policy() -> MilestoneSupervisorPolicy:
    return MilestoneSupervisorPolicy()


# ── Test cases ─────────────────────────────────────────────────────────────

def test_stuck_instruction_blocked_immediately() -> None:
    """An instruction that led to stuck should be blocked on first repeat."""
    p = _policy()
    history = [
        _turn("点击微信图标", summary=""),
        _turn("重试中", summary="卡住：打开错误应用"),
    ]
    result = p._is_repeated_instruction("点击微信图标", MID, history)
    _report("stuck指令-应立即拦截", result, "not blocked" if not result else "")


def test_stuck_similar_rephrase_blocked() -> None:
    """A rephrased version of a stuck instruction should also be blocked."""
    p = _policy()
    history = [
        _turn("点击底部 Dock 栏中的微信应用图标", summary=""),
        _turn("重试", summary="卡住：图标不可见"),
    ]
    result = p._is_repeated_instruction("点击屏幕底部 Dock 栏中的绿色微信图标", MID, history)
    _report("stuck指令-语义相似改写也应拦截", result, "not blocked" if not result else "")


def test_persistent_failure_blocked_after_2() -> None:
    """An instruction tried 2+ times without stuck should still be blocked."""
    p = _policy()
    history = [
        _turn("点击底部 Dock 栏中的微信应用图标", summary="打开最近通话"),
        _turn("按 Home 键返回主屏幕", summary=""),
        _turn("点击底部 Dock 栏中的微信应用图标", summary="打开 Pages"),
        _turn("按 Home 键返回主屏幕", summary=""),
    ]
    result = p._is_repeated_instruction("点击屏幕底部 Dock 栏中的绿色微信图标", MID, history)
    _report("反复失败(2次)-应拦截", result, "not blocked" if not result else "")


def test_persistent_failure_1_attempt_allowed() -> None:
    """An instruction tried only once without stuck should NOT be blocked."""
    p = _policy()
    history = [
        _turn("点击微信图标", summary="打开最近通话"),
        _turn("按 Home 键返回主屏幕", summary=""),
    ]
    result = p._is_repeated_instruction("点击微信图标", MID, history)
    _report("仅1次失败-应放行", not result, "blocked incorrectly" if result else "")


def test_unrelated_instruction_allowed() -> None:
    """A completely different instruction should always be allowed."""
    p = _policy()
    history = [
        _turn("点击微信图标", summary=""),
        _turn("重试中", summary="卡住"),
    ]
    result = p._is_repeated_instruction("点击底部搜索栏", MID, history)
    _report("不相关指令-应放行", not result, "blocked incorrectly" if result else "")


def test_different_milestone_not_interfering() -> None:
    """Instructions from a different milestone should not affect detection."""
    p = _policy()
    other_mid = "m2"
    history = [
        _turn("点击微信图标", summary="", milestone_id=other_mid),
        _turn("重试中", summary="卡住", milestone_id=other_mid),
    ]
    result = p._is_repeated_instruction("点击微信图标", MID, history)
    _report("不同milestone的指令-不应互相影响", not result, "cross-milestone interference" if result else "")


def test_session_20260526_160642_scenario() -> None:
    """Simulate the real scenario: tap dock → wrong app → home → repeat.

    The third attempt to tap dock should be detected as repeated.
    """
    p = _policy()
    history = [
        _turn("点击底部 Dock 栏中的微信应用图标"),
        _turn("按 Home 键返回主屏幕", summary=""),
        _turn("点击弹窗中的「好」按钮"),
        _turn("点击底部 Dock 栏中的微信应用图标"),
        _turn("按 Home 键返回主屏幕", summary=""),
    ]
    result = p._is_repeated_instruction("点击屏幕底部 Dock 栏中的绿色微信图标", MID, history)
    _report("真实场景-dock点击微信图标已失败2次-应拦截", result, "not blocked" if not result else "")


def test_scroll_not_blocked() -> None:
    """Repeated scroll instructions should NOT be blocked (mode 2 skips them)."""
    p = _policy()
    history = [
        _turn("滚动查看更多账单记录"),
        _turn("滚动查看更多账单记录"),
        _turn("滚动查看更多账单记录"),
    ]
    result = p._is_repeated_instruction("滚动查看更多账单记录", MID, history)
    _report("scroll指令-多次重复不应拦截", not result, "blocked incorrectly" if result else "")


def test_drag_not_blocked() -> None:
    """Repeated drag instructions should NOT be blocked (mode 2 skips them)."""
    p = _policy()
    history = [
        _turn("向上拖动日期选择器的日期列"),
        _turn("向上拖动日期选择器的日期列"),
    ]
    result = p._is_repeated_instruction("向上拖动日期选择器的日期列", MID, history)
    _report("drag指令-多次重复不应拦截", not result, "blocked incorrectly" if result else "")


def test_scroll_still_blocked_if_stuck() -> None:
    """Scroll that led to stuck should still be blocked (mode 1 applies to all)."""
    p = _policy()
    history = [
        _turn("滚动查看更多消息"),
        _turn("重试中", summary="卡住：列表到底"),
    ]
    result = p._is_repeated_instruction("滚动查看更多消息", MID, history)
    _report("scroll导致stuck-仍应拦截", result, "not blocked" if not result else "")


def main() -> int:
    print("── Repeat Detection Eval ──")
    test_stuck_instruction_blocked_immediately()
    test_stuck_similar_rephrase_blocked()
    test_persistent_failure_blocked_after_2()
    test_persistent_failure_1_attempt_allowed()
    test_unrelated_instruction_allowed()
    test_different_milestone_not_interfering()
    test_session_20260526_160642_scenario()
    test_scroll_not_blocked()
    test_drag_not_blocked()
    test_scroll_still_blocked_if_stuck()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
