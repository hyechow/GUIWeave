"""Android checker eval: validates SingleCheck status against labeled cases.

Mirrors evals/iphone/checker/test_checker.py but drives the android checker (injects
``ANDROID_MILESTONE_PROMPTS`` into ``run_checker``).

Seeded from the 2026-06-10 alarm run (logs/.../android/20260610_220003) where the checker
HALLUCINATED a wheel time picker's value: the screen showed 下午 06:00 (minute 00, PM) but
the checker declared "时间已调整为06:30" and marked the set-time milestone done, jumping to
the save step and derailing the whole run. These cases lock in: read the picker value
faithfully — a value that is not 06:30 must stay in_progress, never a hallucinated done.

Run:  uv run python evals/android/checker/test_checker.py
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.model_io import run_checker
from gui_agent.adapters.android.supervisor.milestone.prompts import ANDROID_MILESTONE_PROMPTS

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:58s}"
    if detail:
        line += f"  {detail}"
    print(line)


def test_checker() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        png = (PROJECT_ROOT / c["screenshot"]).read_bytes()
        observation = Observation(png_bytes=png, source="eval")
        m = c["milestone"]
        milestone = Milestone.model_validate({**m, "id": c["label"]})

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                result = run_checker(
                    milestone, observation, [],
                    app_name=m.get("app_name", ""),
                    task_type=m.get("task_type", "action"),
                    constraints=c.get("constraints", []),
                    prompts=ANDROID_MILESTONE_PROMPTS,
                )
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        expected = c["expected"]
        details = []
        if result.status != expected["status"]:
            details.append(f'status: expected {expected["status"]!r}, got {result.status!r}')
        if "reason_contains" in expected and not any(kw in result.reason for kw in expected["reason_contains"]):
            details.append(f'reason should contain one of {expected["reason_contains"]}, got: {result.reason[:100]}')

        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       reason: {result.reason[:200]}")


def main() -> int:
    print("── Android Checker Eval ──")
    test_checker()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
