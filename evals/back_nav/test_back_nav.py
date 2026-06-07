"""Back navigation eval: make_nav_context correctness + infer_back_action accuracy."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from gui_agent.adapters.iphone.recon.back_nav import infer_back_action, make_nav_context

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
    """Validate tap coordinate falls in the expected screen zone."""
    if zone == "bottom_tab":
        return back_y > 800
    if zone == "top_tab":
        return 50 < back_y < 600
    if zone == "back_btn":
        return back_x < 250 and back_y < 300
    if zone == "popup_close":
        return back_x > 0 and back_y > 0  # just check valid coords
    return True


def main() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))

    print(f"\n=== Back Nav Eval ({len(cases)} cases) ===\n")

    # ── Part 1: make_nav_context (unit test, no LLM) ────────────────────────
    print("── make_nav_context (unit test) ──")
    for label, etype, name, expected in [
        ("tab→不包含底部", "tab", "通讯录", "点击了tab「通讯录」"),
        ("tab→不包含底部", "tab", "精选", "点击了tab「精选」"),
        ("button", "button", "返回", "点击了button「返回」"),
        ("icon", "icon", "搜索", "点击了icon「搜索」"),
    ]:
        got = make_nav_context(name, etype)
        ok = got == expected
        _report(f"make_nav_context({name}, {etype})", ok, got, expected)
        if "底部" in got:
            _report(f"  !! 不应包含「底部」", False, got, "无「底部」")

    # ── Part 2: infer_back_action (LLM) ──────────────────────────────────────
    print(f"\n── infer_back_action (LLM, {len(cases)} cases) ──")
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
        exp = c["expected"]

        action = infer_back_action(before_png, after_png, nav_context=nav_context)

        # -- check page_type --
        if action is None:
            _report(f"{c['id']}: page_type", not exp["can_go_back"], "None",
                    exp["page_type"], c.get("note", ""))
            continue

        got_type = action.page_type
        ok_type = got_type == exp["page_type"]
        _report(f"{c['id']}: page_type", ok_type, got_type, exp["page_type"],
                f"({action.back_x:.0f},{action.back_y:.0f}) {action.method}")

        # -- check y_zone --
        zone = exp.get("y_zone", "")
        if zone:
            ok_zone = _check_y_zone(action.back_x, action.back_y, zone)
            _report(f"{c['id']}: y_zone={zone}", ok_zone,
                    f"({action.back_x:.0f},{action.back_y:.0f})", zone)

    elapsed = time.time() - t_start
    print(f"  (LLM calls: {elapsed:.1f}s total)")

    # ── Summary ──────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'─' * 60}")
    print(f"  PASSED {passed}/{total}   FAILED {failed}/{total}")
    print(f"{'─' * 60}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
