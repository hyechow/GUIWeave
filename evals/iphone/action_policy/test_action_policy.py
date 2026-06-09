"""Action policy eval runner: validates StructuredOutputPolicy decisions against labeled cases."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from gui_agent.core.schemas import Observation
from gui_agent.adapters.iphone.policies.structured_output import StructuredOutputPolicy

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:50s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_action(action, instruction: str, expected: dict) -> list[str]:
    details = []
    for key, expected_val in expected.items():
        actual = getattr(action, key, None)
        if isinstance(expected_val, list):
            if actual not in expected_val:
                details.append(f"{key}: expected one of {expected_val}, got {actual!r}")
        elif actual != expected_val:
            details.append(f"{key}: expected {expected_val!r}, got {actual!r}")
    return details


def test_action_policy() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if not cases:
        print("  (no cases)")
        return

    policy = StructuredOutputPolicy()
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue

        png_bytes = screenshot_path.read_bytes()
        obs = Observation(png_bytes=png_bytes, source="eval")

        try:
            decision = policy.decide(obs, c["instruction"], verbose=False)
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        expected = c["expected"]
        # Only pass action-field checks to _check_action; custom checks handled below.
        _meta_keys = {"description_must_not_contain", "not_found"}
        action_expected = {k: v for k, v in expected.items() if k not in _meta_keys}
        details = _check_action(decision.action, c["instruction"], action_expected)

        # Check description must_not_contain (regex patterns)
        for pat in expected.get("description_must_not_contain", []):
            import re
            if re.search(pat, decision.action.description or ""):
                details.append(f"description must not match '{pat}'")

        if "not_found" in expected:
            has_not_found = bool(decision.not_found_reason)
            if expected["not_found"] != has_not_found:
                details.append(
                    f"not_found: expected {expected['not_found']}, got {has_not_found}"
                )

        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            actual = decision.action.model_dump(exclude_none=True)
            print(f"       actual: {json.dumps(actual, ensure_ascii=False)}")


def main():
    print("── Action Policy Eval ──")
    test_action_policy()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
