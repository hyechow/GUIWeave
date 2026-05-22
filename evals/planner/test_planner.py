"""Planner eval runner: validates next-step instruction type against labeled cases."""

import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.supervisor.milestone import PLAN_PROMPT, _PlanResult

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


def _run_planner(screenshot_path: str, milestone: dict, checker: dict) -> _PlanResult:
    png_bytes = (PROJECT_ROOT / screenshot_path).read_bytes()
    img_b64 = base64.b64encode(png_bytes).decode()

    prompt = PLAN_PROMPT.format(
        milestone_name=milestone["name"],
        milestone_desc=milestone["description"],
        success_condition=milestone["success_condition"],
        milestone_kind=milestone["kind"],
        constraints="[]",
        check_status=checker["status"],
        check_reason=checker["reason"],
        issues=json.dumps(checker.get("issues", []), ensure_ascii=False),
        missing_evidence=json.dumps(checker.get("missing_evidence", []), ensure_ascii=False),
        check_summary=checker["summary"],
        history_text="（无历史操作）",
    )

    cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(model=cfg.model, base_url=cfg.base_url, api_key=cfg.api_key, temperature=0)
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    msgs = [
        SystemMessage(content=f"{prompt}\n\n当前日期：{today}"),
        HumanMessage(content=[
            {"type": "text", "text": "请根据当前屏幕做出决策。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ]),
    ]
    return invoke_structured(llm, msgs, _PlanResult)


def _check_instruction(instruction: str, expected: dict) -> list[str]:
    details = []
    must_contain = expected.get("must_contain", [])
    if must_contain and not any(kw in instruction for kw in must_contain):
        details.append(f"must contain one of {must_contain}")

    for pattern in expected.get("must_not_contain", []):
        if re.search(pattern, instruction):
            details.append(f"must not match '{pattern}'")

    return details


def test_planner() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        try:
            result = _run_planner(c["screenshot"], c["milestone"], c["checker"])
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       instruction: {result.instruction}")


def main():
    print("── Planner Eval ──")
    test_planner()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
