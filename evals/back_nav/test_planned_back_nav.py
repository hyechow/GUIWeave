"""Back nav eval: direct Action Policy accuracy (no Planner).

For each case:
- tab cases (page_type B): instruction = "点击[位置]tab栏中的「{selected_tab}」"
  selected_tab is read from cases.json (field "selected_tab") when available,
  otherwise falls back to a generic "find the original tab" instruction.
- back cases (page_type C/A): instruction = 找出口 prompt
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.adapters.iphone.policies.structured_output import StructuredOutputPolicy
from policy_expr.adapters.iphone.recon.planned_back_nav import build_back_instruction
from policy_expr.schemas import Observation
from policy_expr.config import resolve_llm_config

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


def _check_y_zone(back_x: float, back_y: float, zone: str) -> bool:
    if zone == "bottom_tab":
        return back_y > 800
    if zone == "top_tab":
        return 50 < back_y < 600
    if zone == "back_btn":
        return back_x < 250 and back_y < 300
    if zone == "popup_close":
        return back_x > 0 and back_y > 0
    if zone == "popup_confirm":
        return back_y > 400 and back_x > 100
    return True


def _make_instruction(c: dict) -> str:
    nav_context = c.get("nav_context", "")
    selected_tab = c.get("selected_tab", "")
    return build_back_instruction(nav_context, selected_tab)


def main() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    action_policy = StructuredOutputPolicy()

    action_cfg = resolve_llm_config("action_policy")
    print(f"\n=== Back Nav Direct Eval ({len(cases)} cases) ===")
    print(f"Action Policy: {action_cfg.provider}/{action_cfg.model}\n")

    # group_name → list of per-step ok booleans
    group_results: dict[str, list[bool]] = {}

    t_start = time.time()
    for c in cases:
        after_path = EVAL_DIR / c["after"]
        if not after_path.exists():
            _report(c["id"], False, "SKIP", "-", "file not found")
            continue

        after_png = after_path.read_bytes()
        exp = c["expected"]
        instruction = _make_instruction(c)

        obs = Observation(png_bytes=after_png, source="back_nav_eval")
        decision = action_policy.decide(obs, instruction, verbose=False)

        group = c.get("group")

        if decision.not_found_reason:
            ok = False
            _report(f"{c['id']}: y_zone={exp.get('y_zone', '')}", ok,
                    "not_found", exp.get("y_zone", ""),
                    f"「{instruction[:30]}」→ {decision.not_found_reason}")
            if group:
                group_results.setdefault(group, []).append(ok)
            continue

        action = decision.action
        zone = exp.get("y_zone", "")
        ok = True
        if zone and action.x is not None and action.y is not None:
            ok = _check_y_zone(action.x, action.y, zone)
            _report(f"{c['id']}: y_zone={zone}", ok,
                    f"({action.x:.0f},{action.y:.0f})", zone,
                    f"「{instruction[:30]}」")
        if group:
            group_results.setdefault(group, []).append(ok)

    # Report grouped results
    if group_results:
        print("\n── 多步骤组汇总 ──")
        for group_name, step_oks in group_results.items():
            all_pass = all(step_oks)
            tag = "PASS" if all_pass else "FAIL"
            steps = f"{sum(step_oks)}/{len(step_oks)} 步通过"
            _report(f"[组] {group_name}", all_pass, tag, "全部通过", steps)

    elapsed = time.time() - t_start
    print(f"\n  (Total: {elapsed:.1f}s)")
    total = passed + failed
    print(f"\n{'─' * 60}")
    print(f"  PASSED {passed}/{total}   FAILED {failed}/{total}")
    print(f"{'─' * 60}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
