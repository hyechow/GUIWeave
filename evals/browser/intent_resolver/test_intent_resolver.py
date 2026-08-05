"""LLM eval for the Router's one semantic-supplement responsibility.

Run: uv run python evals/browser/intent_resolver/test_intent_resolver.py
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.router import resolve_intent  # noqa: E402

CASES_FILE = Path(__file__).with_name("cases.json")
FORBIDDEN_EXECUTION_TEXT = ("ctx.", "query(", "filter=", "filters=", "click ", "tap ")


def main() -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    failed = 0
    print("── Semantic Supplement Eval ──")
    for case in cases:
        supplement = resolve_intent(case["goal"]).semantic_supplement
        folded = supplement.casefold()
        failures = []
        expected = bool(case["expect_supplement"])
        if bool(supplement) != expected:
            failures.append(
                "expected a supplement" if expected else "expected an empty supplement"
            )
        failures.extend(
            f"missing {value!r}"
            for value in case.get("contains", [])
            if value.casefold() not in folded
        )
        failures.extend(
            f"missing every alternative in {values!r}"
            for values in case.get("contains_any", [])
            if not any(value.casefold() in folded for value in values)
        )
        failures.extend(
            f"contains forbidden text {value!r}"
            for value in case.get("not_contains", [])
            if value.casefold() in folded
        )
        failures.extend(
            f"contains execution text {value!r}"
            for value in FORBIDDEN_EXECUTION_TEXT
            if value.casefold() in folded
        )
        if supplement and case.get("language") == "zh" and not re.search(
            r"[\u4e00-\u9fff]", supplement
        ):
            failures.append("Chinese source task produced a non-Chinese supplement")
        failed += bool(failures)
        print(f"  [{'FAIL' if failures else 'PASS'}] {case['label']}")
        print(f"        supplement: {supplement!r}")
        if failures:
            print(f"        {'; '.join(failures)}")
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
