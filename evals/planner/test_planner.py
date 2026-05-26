"""Planner eval runner: validates next-step instruction type against labeled cases."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from policy_expr.supervisor.milestone import _PlanResult, _SingleCheckResult, run_planner

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:45s}"
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


def _check_hints(result: _PlanResult, expected: dict) -> list[str]:
    details = []
    if "direction" in expected and result.direction != expected["direction"]:
        details.append(f"direction: expected '{expected['direction']}', got '{result.direction}'")
    if "drag_column" in expected and result.drag_column != expected["drag_column"]:
        details.append(f"drag_column: expected '{expected['drag_column']}', got '{result.drag_column}'")
    if "drag_magnitude" in expected and result.drag_magnitude != expected["drag_magnitude"]:
        details.append(f"drag_magnitude: expected '{expected['drag_magnitude']}', got '{result.drag_magnitude}'")
    return details


def _build_history(milestone_id: str, instructions: list[str]) -> list[PolicyTurn]:
    """Build minimal PolicyTurn history from a list of instruction strings."""
    turns = []
    for i, inst in enumerate(instructions):
        turns.append(PolicyTurn(
            index=i + 1,
            observation_source="eval",
            supervisor=SupervisorStep(
                should_act=True,
                instruction=inst,
                stop=False,
                goal_completed=False,
                summary=inst,
                milestone_id=milestone_id,
            ),
            executed=True,
        ))
    return turns


def test_planner() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        png_bytes = (PROJECT_ROOT / c["screenshot"]).read_bytes()
        observation = Observation(png_bytes=png_bytes, source="eval")
        milestone_data = {**c["milestone"], "id": c["label"]}
        milestone = Milestone.model_validate(milestone_data)
        check = _SingleCheckResult.model_validate({
            **c["checker"],
            "visible_evidence": c["checker"].get("visible_evidence", []),
        })
        history = _build_history(c["label"], c.get("history", []))

        try:
            result = run_planner(
                milestone, check, observation, history,
                constraints=c.get("constraints"),
            )
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        details += _check_hints(result, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       instruction: {result.instruction}")
            print(f"       direction={result.direction}, column={result.drag_column}, magnitude={result.drag_magnitude}")


def main():
    print("── Planner Eval ──")
    test_planner()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
