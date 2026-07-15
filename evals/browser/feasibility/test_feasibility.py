"""Browser feasibility eval: validates the Feasibility Guard's verdict against labeled cases.

The Feasibility Guard judges from the page's CONTROL INVENTORY (text from DOM form_controls + grid
facts), NOT pixels — so cases are pure text (statement_goal + control_text + optional knowledge), no
screenshots. Each case asserts the verdict's `feasible` bool, and optionally substrings the reason
must contain / the directive must not contain.

Seeded from two live runs:
  - 20260622_104201: a Reviews list has no Rating filter control → infeasible (the real win: kick back
    so the orchestrator re-plans to read ratings per-row instead of filtering by a control that isn't
    there).
  - 20260622_115356: a NAVIGATION statement ('go to the Product Reviews list') was (mis)judged by
    asking "is the destination's review-list control on the CURRENT page?" — on the Dashboard it never
    is (that's why you navigate), so this must NOT be read as infeasible. Guards the nav mis-fire.

Run:  uv run python evals/browser/feasibility/test_feasibility.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.supervisor.statement.feasibility import judge_feasibility

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def test_feasibility() -> None:
    global passed, failed
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        # A case may attach a screenshot — the Feasibility judge reads navigation menus / visible
        # page structure from it (DOM control inventory stays authoritative for control presence).
        image = None
        if c.get("screenshot"):
            sp = PROJECT_ROOT / c["screenshot"]
            if not sp.exists():
                print(f"  [SKIP] {c['label']:64s}  screenshot not found: {c['screenshot']}")
                skipped += 1
                continue
            image = sp.read_bytes()
        try:
            v = judge_feasibility(c["statement_goal"], c["control_text"], c.get("knowledge", ""), image=image)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {c['label']:64s}  exception: {e}")
            continue

        exp = c["expected"]
        details: list[str] = []
        if v.feasible != exp["feasible"]:
            details.append(f"feasible: expected {exp['feasible']}, got {v.feasible}")
        reason_any = exp.get("reason_contains", [])
        if reason_any and not any(kw in (v.reason or "") for kw in reason_any):
            details.append(f"reason should contain one of {reason_any}")
        for kw in exp.get("reason_not_contains", []):
            if kw in (v.reason or ""):
                details.append(f"reason should NOT contain {kw!r}")
        for kw in exp.get("directive_not_contains", []):
            if kw in (v.directive or ""):
                details.append(f"directive should NOT contain {kw!r}")
        if exp.get("directive_nonempty") and not (v.directive or "").strip():
            details.append("directive should be non-empty when infeasible")

        ok = not details
        passed += ok
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {c['label']}")
        if not ok:
            print(f"        {'; '.join(details)}")
            print(f"        verdict: feasible={v.feasible} | reason={(v.reason or '')[:140]}")
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")


def main() -> int:
    print("── Browser Feasibility Eval ──")
    test_feasibility()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
