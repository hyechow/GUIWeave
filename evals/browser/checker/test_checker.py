"""Browser checker eval: validates SingleCheck judgments against labeled cases.

Mirrors evals/android/checker/test_checker.py with BROWSER_MILESTONE_PROMPTS, plus two
browser-specific assertion kinds:

  - ``missing_contains``: a pending field MUST be listed in missing_evidence — the checker
    misleads the planner when it silently treats an uncommitted field as done.
  - ``must_not_claim``: regex patterns the reason/summary must NOT assert.

Seeded from 20260612_104857: a searchable combobox held a typed FILTER word with its
candidate list still open (not selected — moving focus clears it), and the checker claimed
the field was already set, steering the planner into typing the next field and losing the
value repeatedly (6 wasted turns).

Run:  uv run python evals/browser/checker/test_checker.py
"""

import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.self_learning.app_summary import KNOWLEDGE_DIR
from gui_agent.core.supervisor.milestone import run_checker
from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS

CASES_FILE = Path(__file__).parent / "cases.json"


def _app_check_knowledge(c: dict) -> str:
    """Production-parity dynamic acceptance knowledge: when the case sets
    ``with_app_check``, load knowledge/browser/<app_name>/_check.md (machine-local,
    not in git) and inject it the way auto_discover_knowledge → run_checker does.
    Returns "" when absent — the case then runs without it (and may justifiably fail,
    which is the point: the rule lives in the dynamic layer, not the static prompt)."""
    if not c.get("with_app_check"):
        return ""
    p = KNOWLEDGE_DIR / "browser" / c["milestone"].get("app_name", "") / "_check.md"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _build_history(entries: list[dict]) -> list[PolicyTurn]:
    """Reconstruct PolicyTurns from compact case-JSON history entries.

    History is part of the checker's INPUT and can prime hallucinations (20260612_184401:
    with the two dropdown turns in history the checker re-asserted "候选列表仍展开" against
    a screenshot showing it closed — 9/10; with empty history 0/10). Cases that reproduce
    history-primed misjudgments must carry those turns.
    """
    turns = []
    for h in entries:
        sv = SupervisorStep(
            should_act=True, instruction=h["instruction"], stop=False,
            goal_completed=False, summary=h.get("summary", ""),
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
    line = f"  [{tag}] {label:58s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_result(result, expected: dict) -> list[str]:
    details = []
    if "outcome_status" in expected and result.outcome_status != expected["outcome_status"]:
        details.append(
            "outcome_status: expected "
            f"{expected['outcome_status']!r}, got {result.outcome_status!r}"
        )
    # status：单值用 "status"；当一个轨迹对多个状态都合理时（如 0 条结果 + 同时在打转，
    # stuck/in_progress 都不误判成功），用 "status_in" 接受一个集合，真正的锚点交给 must_not_claim。
    if "status_in" in expected:
        if result.status not in expected["status_in"]:
            details.append(
                f'status: expected one of {expected["status_in"]!r}, got {result.status!r}'
            )
    elif result.status != expected["status"]:
        details.append(f'status: expected {expected["status"]!r}, got {result.status!r}')
    if "reason_contains" in expected and not any(
        kw in result.reason for kw in expected["reason_contains"]
    ):
        details.append(f'reason should contain one of {expected["reason_contains"]}')
    for kw in expected.get("missing_contains", []):
        if not any(kw in item for item in result.missing_evidence):
            details.append(
                f"missing_evidence 应包含未决字段「{kw}」, got: {result.missing_evidence}"
            )
    # missing_not_contains：已完成的字段绝不能再进 missing_evidence——checker 的
    # 假阴性会把 planner 推回去「补救」一个不存在的问题（重开下拉框自证循环）
    for kw in expected.get("missing_not_contains", []):
        hits = [item for item in result.missing_evidence if kw in item]
        if hits:
            details.append(f"missing_evidence 不得再含已完成字段「{kw}」, got: {hits}")
    # must_not_claim 同时覆盖 missing_evidence——「索要页面上不存在的控件」正是写在那里
    claims_text = f"{result.reason} {result.summary} " + " ".join(result.missing_evidence)
    for pattern in expected.get("must_not_claim", []):
        if re.search(pattern, claims_text):
            details.append(f"不得宣称/索要 /{pattern}/")
    return details


def test_checker() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            print(f"  [SKIP] {c['label']:58s}  screenshot not found: {c['screenshot']}")
            skipped += 1
            continue
        observation = Observation(
            png_bytes=screenshot_path.read_bytes(),
            source="eval",
            **c.get("observation", {}),
        )
        m = c["milestone"]
        milestone = Milestone.model_validate({**m, "id": c["label"]})
        extra = c.get("extra", "")
        if c.get("with_route_identity_evidence"):
            from gui_agent.core.supervisor.milestone.execution_scope import route_identity_evidence

            hint = route_identity_evidence(milestone, observation)
            extra = f"{extra}\n{hint}".strip()

        expected = c["expected"]
        failures: list[str] = []
        result = None
        attempts = max(1, int(c.get("attempts", 1)))
        for attempt in range(1, attempts + 1):
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    result = run_checker(
                        milestone, observation, _build_history(c.get("history", [])),
                        app_name=m.get("app_name", ""),
                        task_type=m.get("task_type", "action"),
                        constraints=c.get("constraints", []),
                        extra=extra,
                        prompts=BROWSER_MILESTONE_PROMPTS,
                        check_knowledge=_app_check_knowledge(c),
                        state_trace_text=c.get("state_trace", ""),
                        last_action_effect=c.get("last_action_effect", ""),
                    )
            except Exception as e:  # noqa: BLE001
                failures.append(f"attempt {attempt}: exception: {e}")
                continue
            details = _check_result(result, expected)
            if details:
                failures.append(f"attempt {attempt}: " + "; ".join(details))

        ok = not failures
        _report(c["label"], ok, " | ".join(failures))
        if not ok:
            if result is not None:
                print(f"       reason: {result.reason[:160]}")
                print(f"       missing: {result.missing_evidence}")
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")


def main() -> int:
    print("── Browser Checker Eval ──")
    test_checker()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
