"""Back nav2 eval: test planner + action policy pipeline accuracy.

Reuses the same cases.json data as test_back_nav.py.
For each case, invokes the planner to get an instruction, then the action policy
to get coordinates. Validates page_type semantics and y_zone accuracy.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.recon.planned_back_nav import _invoke_planner
from policy_expr.config import resolve_llm_config
from policy_expr.policies.structured_output import StructuredOutputPolicy
from policy_expr.schemas import Observation

CASES_FILE = Path(__file__).parent / "cases.json"
EVAL_DIR = Path(__file__).resolve().parent

passed = 0
failed = 0


def _report(label: str, ok: bool, got, expected, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:52s}  got={str(got):10s}  exp={str(expected):10s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _classify_page_type(instruction: str) -> str:
    """Infer page_type from planner instruction text."""
    inst = instruction.lower()
    # Type A: popup/modal action — must have explicit popup context or popup-specific option
    if any(kw in inst for kw in ("弹窗", "浮层")):
        return "A"
    # Confirmation popup options (不保存/放弃/离开) → popup action
    if any(kw in inst for kw in ("不保存", "放弃修改", "离开", "不保留", "确认离开")):
        return "A"
    # Type B: tab/导航栏 switch (planner uses "tab", "标签", or "导航栏")
    if any(kw in inst for kw in ("tab", "标签", "导航栏")) and not any(
        kw in inst for kw in ("返回", "箭头", "back")
    ):
        return "B"
    # Type C: back/close navigation (back button, page-level close/cancel)
    if any(kw in inst for kw in ("返回", "箭头", "back", "关闭", "取消", "×", "左上角")):
        return "C"
    return "C"  # default to C (most common)


def _check_y_zone(back_x: float, back_y: float, zone: str) -> bool:
    """Validate tap coordinate falls in the expected screen zone."""
    if zone == "bottom_tab":
        return back_y > 800
    if zone == "top_tab":
        return 50 < back_y < 600
    if zone == "back_btn":
        return back_x < 250 and back_y < 300
    if zone == "popup_close":
        return back_x > 0 and back_y > 0
    if zone == "popup_confirm":
        # Confirmation popup buttons are in the lower half of the popup area
        return back_y > 400 and back_x > 100
    return True


def main() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    action_policy = StructuredOutputPolicy()

    print(f"\n=== Back Nav2 Eval ({len(cases)} cases) ===")
    planner_cfg = resolve_llm_config("back_nav")
    action_cfg = resolve_llm_config("action_policy")
    print(f"Planner: {planner_cfg.provider}/{planner_cfg.model}  "
          f"Action: {action_cfg.provider}/{action_cfg.model}\n")

    # ── Part 1: Planner instruction quality ──────────────────────────────
    print("── Planner instructions ──")
    t_start = time.time()
    for c in cases:
        before_path = EVAL_DIR / c["before"]
        after_path = EVAL_DIR / c["after"]
        if not before_path.exists() or not after_path.exists():
            _report(c["id"], False, "SKIP", "-", "file not found")
            continue

        before_png = before_path.read_bytes()
        after_png = after_path.read_bytes()
        nav_context = c.get("nav_context", "")
        target_label = c.get("target_label", "")
        target_description = c.get("target_description", "")
        exp = c["expected"]

        # Planner: sees TARGET (before) + CURRENT (after)
        plan = _invoke_planner(
            target_png=before_png,
            current_png=after_png,
            nav_context=nav_context,
            target_label=target_label,
            target_description=target_description,
        )

        got_type = _classify_page_type(plan.instruction)
        ok_type = got_type == exp["page_type"]
        _report(f"{c['id']}: page_type", ok_type, got_type, exp["page_type"],
                f"「{plan.instruction}」")

        # Action policy: convert instruction to coordinates
        obs = Observation(png_bytes=after_png, source="planned_back_nav_eval")
        decision = action_policy.decide(obs, plan.instruction, verbose=False)

        if decision.not_found_reason:
            _report(f"{c['id']}: y_zone={exp.get('y_zone', '')}", False,
                    "not_found", exp.get("y_zone", ""),
                    decision.not_found_reason)
            continue

        action = decision.action
        zone = exp.get("y_zone", "")
        if zone and action.x is not None and action.y is not None:
            ok_zone = _check_y_zone(action.x, action.y, zone)
            _report(f"{c['id']}: y_zone={zone}", ok_zone,
                    f"({action.x:.0f},{action.y:.0f})", zone,
                    f"「{plan.instruction}」")

    elapsed = time.time() - t_start
    print(f"  (Total: {elapsed:.1f}s)")

    # ── Summary ──────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'─' * 60}")
    print(f"  PASSED {passed}/{total}   FAILED {failed}/{total}")
    print(f"{'─' * 60}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
