"""Checker eval runner: validates SingleCheck status and loading field against labeled cases."""

import base64
import json
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
from policy_expr.supervisor.milestone import SINGLE_CHECKER_PROMPT, _SingleCheckResult

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

passed = 0
failed = 0
fallback_count = 0

_original_invoke = invoke_structured


def _tracked_invoke(llm, messages, schema):
    global fallback_count
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = _original_invoke(llm, messages, schema)
    output = buf.getvalue()
    if "改用纯文本 JSON 解析" in output:
        fallback_count += 1
    print(output, end="")
    return result


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:45s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _run_checker(screenshot_path: str, milestone: dict) -> _SingleCheckResult:
    png_bytes = (PROJECT_ROOT / screenshot_path).read_bytes()
    img_b64 = base64.b64encode(png_bytes).decode()

    prompt = SINGLE_CHECKER_PROMPT.format(
        milestone_name=milestone["name"],
        milestone_desc=milestone["description"],
        success_condition=milestone["success_condition"],
        milestone_kind=milestone["kind"],
        completion_strategy=milestone.get("completion_strategy", ""),
        task_type=milestone.get("task_type", "action"),
        constraints="[]",
        history_text="（无历史操作）",
        app_name=milestone["app_name"],
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
    return _tracked_invoke(llm, msgs, _SingleCheckResult)


def test_checker() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        try:
            result = _run_checker(c["screenshot"], c["milestone"])
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        expected = c["expected"]
        details = []

        if result.status != expected["status"]:
            details.append(f'status: expected {expected["status"]!r}, got {result.status!r}')

        if "loading" in expected and result.loading != expected["loading"]:
            details.append(f'loading: expected {expected["loading"]}, got {result.loading}')

        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       reason: {result.reason[:100]}")


def main():
    print("── Checker Eval ──")
    test_checker()
    fallback_note = f"  ({fallback_count} fallback{'s' if fallback_count != 1 else ''})" if fallback_count else ""
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed{fallback_note}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
