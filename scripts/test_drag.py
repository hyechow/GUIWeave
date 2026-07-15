"""Functional test for drag gestures and action-policy drag output.

Run while the phone is on a picker UI such as WeChat bill month/year selector.

Usage:
    uv run python scripts/test_drag.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui_agent.adapters.iphone.executor import ActionExecutor
from gui_agent.adapters.iphone.perception import LivePhoneSession
from gui_agent.core.policies import StructuredOutputPolicy
from gui_agent.core.schemas import Action, ActionDecision, StatementContract, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.statement.model_io import run_checker, run_planner


OUT_DIR = Path("/tmp/drag_policy_test")

# Known-good picker gesture for the WeChat bill year column:
# start on the selected year row, drag downward inside the same column.
DIRECT_DRAG = ActionDecision(
    action=Action(
        action_type="drag",
        x=270,
        y=635,
        to_x=270,
        to_y=820,
        duration_ms=1800,
        description="直接拖动年份栏向下",
    )
)

POLICY_INSTRUCTION = "把年份栏下拉"
LEVEL3_CONSTRAINTS = [
    "滚轮选择器实测：左侧年份列向上拖动会让年份数字增大，向下拖动会让年份数字减小。",
    "如果当前年份大于目标年份，必须向下拖动左侧年份列，或点击可见的目标年份；不要继续向上拖动。",
    "如果当前年份小于目标年份，才向上拖动左侧年份列。",
    "滚轮选择器实测：右侧月份列向上拖动会让月份数字增大，向下拖动会让月份数字减小。",
    "如果当前月份大于目标5月，必须向下拖动右侧月份列，或点击可见的5月；不要继续向上拖动。",
    "如果当前月份小于目标5月，才向上拖动右侧月份列。",
]
LEVEL4_CONSTRAINTS = [
    *LEVEL3_CONSTRAINTS,
    "滚轮选择器实测：日期列向上拖动会让日期数字增大，向下拖动会让日期数字减小。",
    "如果当前日期大于目标日期，必须向下拖动日期列；如果当前日期小于目标日期，才向上拖动。",
    "设置开始日期后，必须先点击右侧「轻触选择日期」区域激活结束日期输入框，才能拖动日期列设置结束日期。",
    "日期范围筛选必须使用「选择时间段」标签，不要停留在「选择月份」。",
    "如果目标日期在当前屏幕可见，必须直接点击该日期，禁止继续拖动。",
    "设置结束日期前，必须确认开始日期已经是2026年5月19日。",
    "完成后不要点击取消；需要应用筛选时点击绿色「确定」。",
]

LEVEL3_STATEMENT = StatementContract(
    id="drag_level3_target_month",
    name="选择目标月份",
    description=(
        "在微信账单月份选择器中选中 2025年5月。"
        "如果年份不正确，拖动左侧年份列直到 2025年 位于高亮选中行；不要只点击当前错误年份。"
    ),
    success_condition="当前月份选择器的高亮选中行为 2025年 和 5月",
    kind="filter",
    completion_strategy="visible_once",
    depends_on=[],
)

LEVEL4_STATEMENT = StatementContract(
    id="drag_level4_date_range",
    name="选择目标时间段",
    description=(
        "在微信账单筛选弹窗中切换到「选择时间段」，"
        "设置开始日期为2026年5月19日，结束日期为2026年5月25日。"
        "如果当前在「选择月份」页，先点击「选择时间段」标签。"
    ),
    success_condition=(
        "当前筛选弹窗显示开始日期为2026年5月19日，"
        "结束日期为2026年5月25日，且两个日期都已选中或填入。"
    ),
    kind="filter",
    completion_strategy="visible_once",
    depends_on=[],
)


def test_direct_drag(phone: LivePhoneSession, executor: ActionExecutor) -> bool:
    """Execute a known-good drag action directly."""
    print("=== Level 1: test_direct_drag ===")
    before = phone.screenshot()
    (OUT_DIR / "direct_before.png").write_bytes(before)
    executor.execute(DIRECT_DRAG)
    time.sleep(1.0)
    after = phone.screenshot()
    (OUT_DIR / "direct_after.png").write_bytes(after)
    print(f"saved: {OUT_DIR / 'direct_before.png'}")
    print(f"saved: {OUT_DIR / 'direct_after.png'}")
    print("Level 1 PASS")
    return True


def test_action_policy_drag(phone: LivePhoneSession, executor: ActionExecutor) -> bool:
    """Ask action policy to produce a drag action, then execute it."""
    print("\n=== Level 2: test_action_policy_drag ===")
    png = phone.screenshot()
    (OUT_DIR / "policy_input.png").write_bytes(png)
    observation = Observation(png_bytes=png, source="live")
    decision = StructuredOutputPolicy().decide(observation, POLICY_INSTRUCTION)
    action = decision.action

    print(f"instruction: {POLICY_INSTRUCTION}")
    print(f"action_type: {action.action_type}")
    print(f"x/y:         {action.x}, {action.y}")
    print(f"to_x/to_y:   {action.to_x}, {action.to_y}")
    print(f"duration_ms: {action.duration_ms}")
    print(f"description: {action.description}")

    if action.action_type != "drag":
        print("Level 2 FAIL: action policy did not choose drag")
        return False

    executor.execute(decision)
    time.sleep(1.0)
    policy_after = phone.screenshot()
    (OUT_DIR / "policy_after.png").write_bytes(policy_after)
    print(f"saved: {OUT_DIR / 'policy_input.png'}")
    print(f"saved: {OUT_DIR / 'policy_after.png'}")
    print("Level 2 PASS")
    return True


def test_magnitude_calibration(phone: LivePhoneSession, executor: ActionExecutor) -> bool:
    """Calibrate _PICKER_ROW_NORM: drag 1, 2, 3 steps and confirm actual rows moved.

    Expects the phone to be on a picker (e.g. WeChat date picker with the day column visible).
    Drags the day column upward for each step count, then drags back to reset.

    Run with: uv run python scripts/test_drag.py --start 2
    """
    from gui_agent.adapters.iphone.policies.structured_output import _PICKER_ROW_NORM

    print("\n=== Level 2b: picker row calibration ===")
    print(f"  _PICKER_ROW_NORM = {_PICKER_ROW_NORM}")
    input("  请确保手机在日期选择器页面（日期列可见），按回车开始...")

    y_center = 635
    for steps in (1, 2, 3):
        delta_px = steps * _PICKER_ROW_NORM
        executor.execute(ActionDecision(
            action=Action(
                action_type="drag",
                x=254,
                y=y_center,
                to_x=254,
                to_y=y_center - delta_px,
                duration_ms=1000,
                description=f"steps={steps} delta={delta_px}px UP",
            )
        ))
        time.sleep(1.5)
        print(f"  [steps={steps}] delta={delta_px:3d}px UP done")
        input(f"  → 移动了几格？（预期 {steps} 格）按回车继续...")

        executor.execute(ActionDecision(
            action=Action(
                action_type="drag",
                x=254,
                y=y_center,
                to_x=254,
                to_y=y_center + delta_px,
                duration_ms=1000,
                description=f"steps={steps} delta={delta_px}px DOWN (reset)",
            )
        ))
        time.sleep(1.5)
        input("  → 已复位，按回车继续下一个...")

    print("Level 2b DONE")
    return True


def test_planner_checker_target_date(phone: LivePhoneSession, executor: ActionExecutor) -> bool:
    """Run checker -> planner -> action policy -> checker for a target picker value."""
    return _run_planner_checker_loop(
        phone=phone,
        executor=executor,
        statement=LEVEL3_STATEMENT,
        constraints=LEVEL3_CONSTRAINTS,
        label="Level 3",
        max_steps=8,
    )


def test_planner_checker_date_range(phone: LivePhoneSession, executor: ActionExecutor) -> bool:
    """Run checker/planner/action-policy loop for a concrete date range."""
    return _run_planner_checker_loop(
        phone=phone,
        executor=executor,
        statement=LEVEL4_STATEMENT,
        constraints=LEVEL4_CONSTRAINTS,
        label="Level 4",
        max_steps=20,
    )


def _run_planner_checker_loop(
    *,
    phone: LivePhoneSession,
    executor: ActionExecutor,
    statement: StatementContract,
    constraints: list[str],
    label: str,
    max_steps: int,
) -> bool:
    print(f"\n=== {label}: checker + planner + action policy ===")
    history: list[PolicyTurn] = []
    policy = StructuredOutputPolicy()

    for step_no in range(1, max_steps + 1):
        png = phone.screenshot()
        prefix = label.lower().replace(" ", "")
        shot_path = OUT_DIR / f"{prefix}_step{step_no}_before.png"
        shot_path.write_bytes(png)
        observation = Observation(png_bytes=png, source="live")

        check = run_checker(
            statement,
            observation,
            history,
            app_name="微信",
            task_type="action",
        )
        print(f"\n[{label} step {step_no}] checker: {check.status} | {check.reason}")
        if check.status == "done":
            print(f"{label} PASS")
            return True

        plan = run_planner(
            statement,
            check,
            observation,
            history,
            constraints=constraints,
        )
        print(f"[{label} step {step_no}] planner_instruction: {plan.instruction}")
        print(f"[{label} step {step_no}] planner_summary:     {plan.summary}")
        print(f"[{label} step {step_no}] hint_direction:      {plan.direction}")
        print(f"[{label} step {step_no}] hint_drag_column:    {plan.drag_column}")
        if not plan.instruction:
            print(f"{label} FAIL: planner returned empty instruction")
            return False

        decision = policy.decide(
            observation, plan.instruction,
            direction=plan.direction,
            drag_column=plan.drag_column,
        )
        action = decision.action
        print(f"[{label} step {step_no}] action_type: {action.action_type}")
        print(f"[{label} step {step_no}] x/y:         {action.x}, {action.y}")
        print(f"[{label} step {step_no}] to_x/to_y:   {action.to_x}, {action.to_y}")
        print(f"[{label} step {step_no}] duration_ms: {action.duration_ms}")
        print(f"[{label} step {step_no}] description: {action.description}")

        executed = executor.execute(decision)
        history.append(_policy_turn(step_no, plan.instruction, plan.summary, decision, executed, statement))
        time.sleep(1.0)

    final = phone.screenshot()
    final_path = OUT_DIR / f"{label.lower().replace(' ', '')}_final.png"
    final_path.write_bytes(final)
    print(f"saved: {final_path}")
    print(f"{label} FAIL: target was not selected within step budget")
    return False


def _policy_turn(
    index: int,
    instruction: str,
    summary: str,
    decision: ActionDecision,
    executed: bool,
    statement: StatementContract,
) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="live",
        supervisor=SupervisorStep(
            should_act=True,
            instruction=instruction,
            summary=summary,
            statement_id=statement.id,
            statement_kind=statement.kind,
            completion_strategy=statement.completion_strategy,
        ),
        action_decision=decision,
        executed=executed,
    )


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, choices=[1, 2, 3, 4],
                        help="从指定 level 开始测试（默认 1）")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    passed: list[str] = []
    with LivePhoneSession() as phone:
        executor = ActionExecutor(phone=phone)
        if args.start <= 1:
            if not test_direct_drag(phone, executor):
                print("\nResult: FAIL at Level 1")
                return 1
            passed.append("Level 1")
        if args.start <= 2:
            if not test_action_policy_drag(phone, executor):
                print("\nResult: FAIL at Level 2")
                return 1
            passed.append("Level 2")
            if not test_magnitude_calibration(phone, executor):
                print("\nResult: FAIL at Level 2b")
                return 1
            passed.append("Level 2b")
            exit(0)
        if args.start <= 3:
            if not test_planner_checker_target_date(phone, executor):
                print("\nResult: FAIL at Level 3")
                return 1
            passed.append("Level 3")
        if not test_planner_checker_date_range(phone, executor):
            print("\nResult: FAIL at Level 4")
            return 1
        passed.append("Level 4")
    print(f"\nResult: PASS {' + '.join(passed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
