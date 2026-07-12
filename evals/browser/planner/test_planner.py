"""Browser planner eval: validates the next-step instruction against labeled cases.

Mirrors evals/iphone/planner/test_planner.py but drives the BROWSER milestone
prompts (``BROWSER_MILESTONE_PROMPTS``) through the shared ``run_planner``.

Seeded from the 2026-06-09 "打开百度首页" run where the planner told the action
policy to "在搜索框中输入文字 baidu.com" (→ a page-search type) instead of a
navigate-style instruction. The fix lives in the browser PLAN_PROMPT; this guards it.
Run:  uv run python evals/browser/planner/test_planner.py
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone import _SingleCheckResult, run_planner
from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS

CASES_FILE = Path(__file__).parent / "cases.json"


def _build_history(entries: list[dict], milestone_id: str) -> list[PolicyTurn]:
    """Reconstruct PolicyTurns from compact case-JSON history entries (mirrors the
    checker eval). History lets the planner reason about already-executed turns."""
    turns = []
    for h in entries:
        sv = SupervisorStep(
            should_act=True, instruction=h["instruction"], stop=False,
            goal_completed=False, summary=h.get("summary", ""),
            milestone_id=h.get("milestone_id", milestone_id),
        )
        ad = BrowserActionDecision.model_validate({"action": h["action"]}) if h.get("action") else None
        turns.append(PolicyTurn(
            index=h.get("index", len(turns) + 1), observation_source="eval",
            supervisor=sv, action_decision=ad, executed=h.get("executed", True),
        ))
    return turns

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:60s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_instruction(instruction: str, expected: dict) -> list[str]:
    details = []
    must_contain = expected.get("must_contain", [])
    if must_contain and not any(kw in instruction for kw in must_contain):
        details.append(f"must contain one of {must_contain}")
    for kw in expected.get("must_contain_all", []):
        if kw not in instruction:
            details.append(f"must contain '{kw}'")
    for pattern in expected.get("must_match", []):
        if not re.search(pattern, instruction):
            details.append(f"must match '{pattern}'")
    for pattern in expected.get("must_not_contain", []):
        if re.search(pattern, instruction):
            details.append(f"must not match '{pattern}'")
    alternatives = expected.get("any_of", [])
    if alternatives:
        alt_failures = []
        for alt in alternatives:
            alt_details = _check_instruction(instruction, alt)
            if not alt_details:
                break
            label = alt.get("label") or f"alternative {len(alt_failures) + 1}"
            alt_failures.append(f"{label}: {', '.join(alt_details)}")
        else:
            details.append("must satisfy one alternative: " + " | ".join(alt_failures))
    return details


def test_planner() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            print(f"  [SKIP] {c['label']:60s}  screenshot not found: {c['screenshot']}")
            skipped += 1
            continue

        observation = Observation(
            png_bytes=screenshot_path.read_bytes(),
            source="eval",
            **c.get("observation", {}),
        )
        milestone = Milestone.model_validate({**c["milestone"], "id": c["label"]})
        check = _SingleCheckResult.model_validate({
            **c["checker"],
            "visible_evidence": c["checker"].get("visible_evidence", []),
        })

        try:
            result = run_planner(
                milestone, check, observation,
                _build_history(c.get("history", []), milestone.id),
                constraints=c.get("constraints"),
                prompts=BROWSER_MILESTONE_PROMPTS,
            )
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        for field in ("atomic_role", "action_family", "target_control"):
            if field in c["expected"]:
                actual = getattr(result, field, None)
                if actual != c["expected"][field]:
                    details.append(
                        f"{field}: expected {c['expected'][field]!r}, got {actual!r}"
                    )
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       instruction: {result.instruction}")
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")


def main() -> int:
    print("── Browser Planner Eval ──")
    test_planner()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
