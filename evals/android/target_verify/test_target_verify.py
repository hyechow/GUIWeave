"""Android target-verify eval: validates post-action tap landing checks.

Seeded from logs/.../android/20260611_085000 where turn 2 correctly tapped the
bottom-left 闹钟 tab, but target_verify false-flagged the marker as landing on
the 世界时钟 tab. That false off-target sent turn 3 into replan even though the
screen had already advanced to the alarm page.

Run:  uv run python evals/android/target_verify/test_target_verify.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.target_verify import verify_target

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:72s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_result(result, expected: dict) -> list[str]:
    details: list[str] = []
    expected_on_target = expected.get("on_target")
    if expected_on_target is not None and result.on_target != expected_on_target:
        details.append(f"on_target: expected {expected_on_target!r}, got {result.on_target!r}")

    contains_any = expected.get("actual_element_contains_any") or []
    if contains_any:
        actual = (result.actual_element or "").lower()
        needles = [str(s).lower() for s in contains_any]
        if not any(n in actual for n in needles):
            details.append(
                f"actual_element should contain one of {contains_any!r}, got {result.actual_element!r}"
            )
    return details


def test_target_verify() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if not cases:
        print("  (no cases)")
        return

    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue

        point = c["point"]
        try:
            result = verify_target(
                screenshot_path.read_bytes(),
                float(point["x"]),
                float(point["y"]),
                c["instruction"],
            )
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_result(result, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       actual: {json.dumps(result.model_dump(), ensure_ascii=False)}")


def main() -> int:
    print("── Android Target Verify Eval ──")
    test_target_verify()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
