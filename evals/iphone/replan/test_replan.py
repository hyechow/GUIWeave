"""Replan eval: validates replan instruction after scroll failure."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.policies.base import resize_to_logical_png
from gui_agent.core.schemas import StatementContract, Observation, PolicyTurn, SupervisorStep
from gui_agent.adapters.iphone.supervisor.statement.prompts import REPLAN_PROMPT
from gui_agent.core.supervisor.statement.model_io import _build_msgs, _format_history
from gui_agent.core.supervisor.statement.schemas import _ReplanResult, _SingleCheckResult

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

passed = 0
failed = 0


def _build_history(statement_id: str, instructions: list[str]) -> list[PolicyTurn]:
    turns = []
    for i, inst in enumerate(instructions):
        turns.append(PolicyTurn(
            index=i + 1,
            observation_source="eval",
            supervisor=SupervisorStep(
                should_act=True,
                instruction=inst,
                summary=inst,
                statement_id=statement_id,
            ),
            executed=True,
        ))
    return turns


def _check_instruction(instruction: str, expected: dict) -> list[str]:
    details = []
    must_contain = expected.get("must_contain", [])
    if must_contain and not any(kw in instruction for kw in must_contain):
        details.append(f"must contain one of {must_contain}")
    for pattern in expected.get("must_not_contain", []):
        if re.search(pattern, instruction):
            details.append(f"must not match '{pattern}'")
    return details


def _check_diagnosis(diagnosis: str, expected: dict) -> list[str]:
    details = []
    for kw in expected.get("diagnosis_must_contain", []):
        if kw not in diagnosis:
            details.append(f"diagnosis must contain '{kw}'")
    for pattern in expected.get("diagnosis_must_not_contain", []):
        if re.search(pattern, diagnosis):
            details.append(f"diagnosis must not match '{pattern}'")
    return details


def test_replan() -> None:
    global passed, failed
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)

    for c in cases:
        png_bytes = (PROJECT_ROOT / c["screenshot"]).read_bytes()
        m = c["statement"]
        statement = StatementContract.model_validate({**m, "id": c["label"]})
        history = _build_history(c["label"], c.get("history", []))

        # Build tried_instructions like production replan does
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor and t.supervisor.instruction
            and t.supervisor.statement_id == statement.id
        })
        tried_text = "\n".join(f"  - 「{i}」" for i in tried) if tried else "  （无）"

        prompt = REPLAN_PROMPT.format(
            statement_name=statement.name,
            statement_desc=statement.description,
            success_condition=statement.success_condition,
            stuck_reason=c["stuck_reason"],
            issues=json.dumps(c.get("issues", []), ensure_ascii=False),
            retry_count=0,
            constraints="[]",
            failure_hints="[]",
            completed_statements="  （无）",
            history_text=_format_history(history),
            tried_instructions=tried_text,
        )

        b64_png = __import__("base64").b64encode(resize_to_logical_png(png_bytes)).decode()
        messages = [
            SystemMessage(content=prompt),
            HumanMessage(content=[
                {"type": "text", "text": "请诊断失败原因并生成修复指令。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
            ]),
        ]

        try:
            result = invoke_structured(llm, messages, _ReplanResult)
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {c['label']:45s}  exception: {e}")
            continue

        details = _check_instruction(result.instruction, c["expected"])
        details += _check_diagnosis(result.diagnosis, c["expected"])
        ok = len(details) == 0
        passed += ok
        failed += not ok
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {c['label']:45s}")
        if not ok:
            print(f"        {'; '.join(details)}")
            print(f"        instruction: {result.instruction}")
            print(f"        diagnosis:   {result.diagnosis}")


def main():
    print("── Replan Eval ──")
    test_replan()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
