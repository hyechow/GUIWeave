"""Android planner eval: validates picker planning hints from Android prompts.

Seeded from logs/.../android/20260611_095209 where the alarm time picker kept
overshooting and changing direction/column inconsistently. These cases assert the
planner itself emits structured picker hints; action_policy then enforces them.

Run:  uv run python evals/android/planner/test_planner.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.adapters.android.supervisor.milestone.prompts import ANDROID_MILESTONE_PROMPTS
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone import _PlanResult, _SingleCheckResult, run_planner

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


def _check_instruction(instruction: str, expected: dict) -> list[str]:
    details: list[str] = []
    must_contain = expected.get("must_contain", [])
    if must_contain and not any(kw in instruction for kw in must_contain):
        details.append(f"must contain one of {must_contain!r}")
    for pattern in expected.get("must_not_contain", []):
        if re.search(pattern, instruction):
            details.append(f"must not match {pattern!r}")
    return details


def _check_hints(result: _PlanResult, expected: dict) -> list[str]:
    details: list[str] = []
    for field in ("direction", "drag_column", "drag_current_value", "drag_target_value"):
        if field in expected and getattr(result, field) != expected[field]:
            details.append(f"{field}: expected {expected[field]!r}, got {getattr(result, field)!r}")
    return details


def test_planner() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            print(f"  [SKIP] {c['label']:72s}  screenshot not found: {c['screenshot']}")
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
                milestone,
                check,
                observation,
                [],
                constraints=c.get("constraints"),
                prompts=ANDROID_MILESTONE_PROMPTS,
            )
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        details += _check_hints(result, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       instruction: {result.instruction}")
            print(
                "       "
                f"direction={result.direction}, column={result.drag_column}, "
                f"cur={result.drag_current_value}, tgt={result.drag_target_value}"
            )
    if skipped:
        print(f"  ({skipped} skipped - screenshots not committed to git)")


def main() -> int:
    print("── Android Planner Eval ──")
    test_planner()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
