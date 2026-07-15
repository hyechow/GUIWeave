"""Browser replan eval: validates the replan instruction after a sub-goal stalls.

Mirrors evals/iphone/replan/test_replan.py but drives the BROWSER REPLAN_PROMPT (no
retina downscale — browser screenshots are sent raw, like the browser planner/action
evals).

Seeded from 20260616_200258 Turn5: the replanner had NO upload rule (PLAN_PROMPT did),
so it reframed an upload statement as ``点击上传区域以唤起系统文件选择器`` — a tap that
opens a native OS file chooser the device's file-chooser interceptor cancels (wasted
turn). The fix (REPLAN_PROMPT upload rule, symmetric with PLAN_PROMPT) is guarded here.
Run:  uv run python evals/browser/replan/test_replan.py
"""

import base64
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import StatementContract, PolicyTurn, SupervisorStep
from gui_agent.adapters.browser.supervisor.statement.prompts import REPLAN_PROMPT
from gui_agent.core.supervisor.statement.model_io import _format_history
from gui_agent.core.supervisor.statement.schemas import _ReplanResult

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _build_history(statement_id: str, entries: list) -> list[PolicyTurn]:
    # entries are bare instruction strings (mirror the iphone replan eval).
    turns = []
    for i, inst in enumerate(entries):
        turns.append(
            PolicyTurn(
                index=i + 1,
                observation_source="eval",
                supervisor=SupervisorStep(
                    should_act=True,
                    instruction=inst,
                    summary=inst,
                    statement_id=statement_id,
                ),
                executed=True,
            )
        )
    return turns


def _check_instruction(instruction: str, expected: dict) -> list[str]:
    details = []
    must_contain = expected.get("must_contain", [])
    if must_contain and not any(kw in instruction for kw in must_contain):
        details.append(f"must contain one of {must_contain}")
    for kw in expected.get("must_contain_all", []):
        if kw not in instruction:
            details.append(f"must contain '{kw}'")
    for pattern in expected.get("must_not_contain", []):
        if re.search(pattern, instruction):
            details.append(f"must not match '{pattern}'")
    return details


def test_replan() -> None:
    global passed, failed
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
    skipped = 0

    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            print(f"  [SKIP] {c['label']:60s}  screenshot not found: {c['screenshot']}")
            skipped += 1
            continue

        m = c["statement"]
        statement = StatementContract.model_validate({**m, "id": c["label"]})
        history = _build_history(c["label"], c.get("history", []))

        tried = sorted(
            {
                t.supervisor.instruction
                for t in history
                if t.supervisor
                and t.supervisor.instruction
                and t.supervisor.statement_id == statement.id
            }
        )
        tried_text = "\n".join(f"  - 「{i}」" for i in tried) if tried else "  （无）"

        prompt = REPLAN_PROMPT.format(
            statement_name=statement.name,
            statement_desc=statement.description,
            success_condition=statement.success_condition,
            stuck_reason=c.get("stuck_reason", ""),
            issues=json.dumps(c.get("issues", []), ensure_ascii=False),
            retry_count=getattr(statement, "retry_count", 0),
            constraints=json.dumps(c.get("constraints", []), ensure_ascii=False),
            failure_hints=json.dumps(m.get("failure_hints", []), ensure_ascii=False),
            completed_statements="  （无）",
            history_text=_format_history(history),
            tried_instructions=tried_text,
        )

        b64_png = base64.b64encode(screenshot_path.read_bytes()).decode()
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(
                content=[
                    {"type": "text", "text": "请诊断失败原因并生成修复指令。"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64_png}"},
                    },
                ]
            ),
        ]

        try:
            result = invoke_structured(llm, messages, _ReplanResult)
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {c['label']:60s}  exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        ok = len(details) == 0
        passed += ok
        failed += not ok
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {c['label']:60s}")
        if not ok:
            print(f"        {'; '.join(details)}")
            print(f"        instruction: {result.instruction}")

    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")


def main() -> int:
    print("── Browser Replan Eval ──")
    test_replan()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
