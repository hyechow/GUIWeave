"""Android action policy eval: validates AndroidActionPolicy decisions against labeled cases.

Mirrors evals/browser/action_policy/test_action_policy.py but drives the android
``AndroidActionPolicy`` (vision-only phone prompt).

Seeded from the 2026-06-10 alarm run (logs/.../android/20260610_205402) where a wheel
time picker was mishandled: the policy tapped a picker number (zero-effect -> the runner
flagged the action ineffective and stopped) instead of scrolling the column. These cases
lock in "change a picker value -> scroll, never tap", with a contrast case so the picker
rule doesn't turn every tap into a scroll.

Run:  uv run python evals/android/action_policy/test_action_policy.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Observation
from gui_agent.adapters.android.policies import AndroidActionPolicy

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:64s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_action(action, expected: dict) -> list[str]:
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

    policy = AndroidActionPolicy()
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue

        obs = Observation(png_bytes=screenshot_path.read_bytes(), source="eval")
        hints = c.get("hints") or {}
        try:
            decision = policy.decide(
                obs,
                c["instruction"],
                direction=hints.get("direction"),
                drag_column=hints.get("drag_column"),
                drag_steps=hints.get("drag_steps"),
                verbose=False,
            )
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_action(decision.action, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            actual = decision.action.model_dump(exclude_none=True)
            print(f"       actual: {json.dumps(actual, ensure_ascii=False)}")


def main() -> int:
    print("── Android Action Policy Eval ──")
    test_action_policy()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
