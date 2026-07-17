#!/usr/bin/env python3
"""Live eval for semantic Program hot recompilation.

Run:
  uv run python evals/browser/redecompose/test_redecompose.py
  uv run python evals/browser/redecompose/test_redecompose.py --label route
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.core.orchestrator import Program, redecompose, validate_program


@dataclass(frozen=True)
class Case:
    label: str
    goal: str
    completed: str
    remaining: str
    correction: str


CASES = (
    Case(
        "route",
        "Make the requested setting active for the target record.",
        "The target record was identified.",
        "Reach a UI that exposes the setting, then apply and verify it.",
        "The previous route had no usable control; choose another semantic route.",
    ),
    Case(
        "data-source",
        "Report the names of records whose current score is below 3.",
        "The current page was opened.",
        "Obtain complete record data and derive the requested names.",
        "The previous data context was partial; do not treat it as the complete set.",
    ),
)


def _check(program: Program, case: Case) -> list[str]:
    errors = [f"{issue.code}: {issue}" for issue in validate_program(program)]
    payload = program.model_dump_json().casefold()
    if "body_goal" in payload or "subdecompose" in payload:
        errors.append("hot recompile emitted retired runtime expansion semantics")
    if not program.statements:
        errors.append("replacement Program is empty")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    selected = [case for case in CASES if args.label.casefold() in case.label.casefold()]
    if not selected:
        print(f"no case matches label {args.label!r}")
        return 2
    failed = 0
    for case in selected:
        print(f"\n=== {case.label}")
        try:
            program = redecompose(
                case.goal,
                prior_experience=case.completed,
                remaining_plan=case.remaining,
                corrective_directive=case.correction,
            )
            errors = _check(program, case)
            print(program.model_dump_json(indent=2))
        except Exception as exc:
            errors = [str(exc)]
        if errors:
            failed += 1
            for error in errors:
                print(f"  FAIL: {error}")
        else:
            print("  PASS")
    print(f"\nsemantic recompile eval: {len(selected) - failed}/{len(selected)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
