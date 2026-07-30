"""Android router eval runner."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from gui_agent.core.chat.session import route_message

CASES_FILE = Path(__file__).parent / "cases.json"


def run() -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    failed = 0
    for case in cases:
        result = route_message(
            case["user_msg"],
            case["session"],
            platform="android",
        )
        goal = result.goal or ""
        expected = case["expected"]
        missing = [
            pattern
            for pattern in expected.get("required_patterns", [])
            if not re.search(pattern, goal)
        ]
        forbidden = [
            text
            for text in expected.get("goal_not_contains", [])
            if text in goal
        ]
        ok = bool(goal) and not missing and not forbidden
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {case['label']}: {goal}")
        if missing:
            print(f"         missing patterns: {missing}")
        if forbidden:
            print(f"         forbidden text: {forbidden}")
    print(f"\n{len(cases)} tests: {len(cases) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
