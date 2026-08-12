"""WebArena agent_response synthesis eval: validates task_type classification (RETRIEVE/
MUTATE/NAVIGATE) against the official WebArena-Verified answer key.

Seeded from a live run of shopping_admin task 157 ("View the details of all customers"):
the synthesizer classified it as RETRIEVE, produced no retrieved_data, and got demoted to
NOT_FOUND_ERROR by _finalize_response's RETRIEVE invariant — scoring 0 even though the agent
had correctly navigated to the right page. The official answer key expects NAVIGATE (no
retrieved_data ever required). Root cause: the intent's own verb ("View"/"Go to"/"Show" with
no "Get/Return/How many/..." output-format ask) is enough to tell NAVIGATE from RETRIEVE, but
the system prompt gave the model no criteria to use it. cases.json holds all 55
shopping_admin_hard_tasks intents paired with their real expected task_type from
webarena-verified/assets/dataset/webarena-verified.json, so this suite is a direct regression
gate on the synthesize_system.md prompt fix, not a synthetic guess.

Calls the real synthesis LLM (_synthesize_response) with a minimal, content-free `result`
dict — this suite only asserts on task_type classification, not on retrieved_data accuracy
(there is no real run evidence to retrieve from), so phase/notes are deliberately
generic rather than per-task fixtures.

Run:  uv run python evals/browser/webarena_response/test_response_synthesis.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.adapters.browser.webarena import _synthesize_response
from gui_agent.core.run.result import AgentResult

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    mark = "✓" if ok else "✗"
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))


def main() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for case in cases:
        intent = case["intent"]
        expected = case["expected_task_type"].upper()
        result = AgentResult(
            goal=intent,
            output="",
            summary="The run ended without a deterministic response mapping.",
            phase="failed",
        )
        resp = _synthesize_response(intent, result)
        got = (resp.task_type or "").upper()
        ok = got == expected
        _report(
            f"task {case['task_id']}: {intent[:60]!r}",
            ok,
            f"expected={expected} got={got}" if not ok else "",
        )

    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
