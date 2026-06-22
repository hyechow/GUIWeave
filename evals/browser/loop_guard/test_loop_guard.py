"""Browser LoopGuard evals for task-state repeat detection.

These are deterministic state-trace cases. They intentionally encode the desired
behavior at the action-loop guard boundary, not a checker interpretation of the
rendered state trace.

Run:
  uv run python evals/browser/loop_guard/test_loop_guard.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.core.supervisor.milestone.state_trace import StateTrace, canonical_url

CASES_FILE = Path(__file__).parent / "cases.json"


def _check_case(case: dict) -> list[str]:
    trace = StateTrace()
    state = canonical_url(case["state_url"])
    for prior in case.get("prior", []):
        trace.note(prior["index"], state, prior["instruction"], prior.get("dom_state", ""))

    candidate = case["candidate"]
    hit = trace.repeated(state, candidate["instruction"], candidate.get("dom_state", ""))
    got_repeated = hit is not None
    expected_repeated = bool(case["expected"]["repeated"])
    if got_repeated == expected_repeated:
        return []

    detail = (
        f"repeated: expected {expected_repeated}, got {got_repeated}; "
        f"prior_dom={case.get('prior', [{}])[0].get('dom_state', '')!r}; "
        f"candidate_dom={candidate.get('dom_state', '')!r}"
    )
    if hit is not None:
        detail += f"; matched prior T{hit.index}"
    return [detail]


def main() -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    passed = 0
    failed = 0
    print("-- Browser LoopGuard Eval --")
    for case in cases:
        details = _check_case(case)
        ok = not details
        passed += int(ok)
        failed += int(not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {case['label']}")
        if details:
            print(f"        {'; '.join(details)}")
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
