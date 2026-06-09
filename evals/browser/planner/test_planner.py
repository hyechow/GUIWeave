"""Browser planner eval: validates the next-step instruction against labeled cases.

Mirrors evals/iphone/planner/test_planner.py but drives the BROWSER milestone
prompts (``BROWSER_MILESTONE_PROMPTS``) through the shared ``run_planner``.

Seeded from the 2026-06-09 "打开百度首页" run where the planner told the action
policy to "在搜索框中输入文字 baidu.com" (→ a page-search type) instead of a
navigate-style instruction. The fix lives in the browser PLAN_PROMPT; this guards it.
Run:  uv run python evals/browser/planner/test_planner.py
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone import _SingleCheckResult, run_planner
from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:60s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_instruction(instruction: str, expected: dict) -> list[str]:
    details = []
    must_contain = expected.get("must_contain", [])
    if must_contain and not any(kw in instruction for kw in must_contain):
        details.append(f"must contain one of {must_contain}")
    for pattern in expected.get("must_not_contain", []):
        if re.search(pattern, instruction):
            details.append(f"must not match '{pattern}'")
    return details


def test_planner() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            print(f"  [SKIP] {c['label']:60s}  screenshot not found: {c['screenshot']}")
            skipped += 1
            continue

        observation = Observation(png_bytes=screenshot_path.read_bytes(), source="eval")
        milestone = Milestone.model_validate({**c["milestone"], "id": c["label"]})
        check = _SingleCheckResult.model_validate({
            **c["checker"],
            "visible_evidence": c["checker"].get("visible_evidence", []),
        })

        try:
            result = run_planner(
                milestone, check, observation, [],
                constraints=c.get("constraints"),
                prompts=BROWSER_MILESTONE_PROMPTS,
            )
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       instruction: {result.instruction}")
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")


def main() -> int:
    print("── Browser Planner Eval ──")
    test_planner()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
