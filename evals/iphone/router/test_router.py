"""Router eval runner. Loads cases from cases.json and runs route_message."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from gui_agent.core.chat_session import route_message

CASES_FILE = Path(__file__).parent / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def check(result, expected: dict) -> tuple[bool, str]:
    if "goal" in expected:
        if expected.get("or_clarification") and result.needs_clarification:
            return True, ""
        if expected.get("or_empty") and not result.goal:
            return True, ""
        if not result.goal or expected["goal"] not in result.goal:
            return False, f'expected goal containing "{expected["goal"]}", got "{result.goal}"'
    if "goal_contains_all" in expected:
        missing = [kw for kw in expected["goal_contains_all"] if kw not in (result.goal or "")]
        if missing:
            return False, f'goal missing {missing}, got "{result.goal}"'
    # Structural check: goal must match a regex (e.g. an ISO date range) — used
    # for date-relative cases where hardcoding the resolved dates would expire.
    if "goal_regex" in expected:
        if not result.goal or not re.search(expected["goal_regex"], result.goal):
            return False, f'goal did not match /{expected["goal_regex"]}/, got "{result.goal}"'
    if "goal_not_contains" in expected:
        present = [kw for kw in expected["goal_not_contains"] if kw in (result.goal or "")]
        if present:
            return False, f'goal should not contain {present}, got "{result.goal}"'
    if expected.get("clarification") and not expected.get("or_clarification"):
        if not result.needs_clarification:
            return False, f"expected needs_clarification=true, got goal=\"{result.goal}\""
    if expected.get("empty"):
        if result.goal or result.needs_clarification:
            return False, f"expected empty, got goal=\"{result.goal}\" clarification={result.needs_clarification}"
    return True, ""


def run():
    cases = load_cases()
    passed = failed = 0

    for c in cases:
        prefs_context = c.get("prefs_context", "")
        r = route_message(c["user_msg"], c["session"], prefs_context=prefs_context)
        ok, detail = check(r, c["expected"])
        passed += ok
        failed += not ok

        display = r.goal or ("⚠ " + r.clarification if r.needs_clarification else "(空)")
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {c['label']:20s} → {display}")
        if detail:
            print(f"        {detail}")

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
