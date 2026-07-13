"""Loop-check eval: validates _LoopFrameResult.loading / boundary_reached against
labeled scroll-collection frames.

Regression — session 20260607_105731: the collection loop had no loading detection,
so a stale "加载中" frame right after applying the date filter (still showing
out-of-range rows) was read as data, poisoning the stitch baseline and dropping the
middle of the list (missing 5/24-28, mixed in 5/29). The fix adds a `loading` field to
the loop frame check; this eval asserts the model actually flags it on the real frame.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.model_io import run_loop_check

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:42s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _build_history(milestone_id: str, instructions: list[str]) -> list[PolicyTurn]:
    turns = []
    for i, inst in enumerate(instructions):
        turns.append(PolicyTurn(
            index=i + 1,
            observation_source="eval",
            supervisor=SupervisorStep(
                should_act=True, instruction=inst, stop=False,
                goal_completed=False, summary=inst, milestone_id=milestone_id,
            ),
            executed=True,
        ))
    return turns


def test_loop_check() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            print(f"  [SKIP] {c['label']:42s}  screenshot not found: {c['screenshot']}")
            skipped += 1
            continue
        observation = Observation(png_bytes=screenshot_path.read_bytes(), source="eval")
        milestone = Milestone.model_validate({**c["milestone"], "id": c["label"]})
        history = _build_history(c["label"], c.get("history", []))

        try:
            result = run_loop_check(
                milestone, observation, history,
                constraints=c.get("constraints", []),
            )
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        expected = c["expected"]
        details = []
        for field in ("loading", "boundary_reached", "should_stop"):
            if field in expected and getattr(result, field) != expected[field]:
                details.append(f"{field}: expected {expected[field]}, got {getattr(result, field)}")
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       summary: {result.summary[:120]}")
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")


def main() -> int:
    print("── Loop-Check Eval ──")
    test_loop_check()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
