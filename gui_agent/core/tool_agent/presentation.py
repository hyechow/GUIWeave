"""User-facing presentation for verified Tool Agent execution results.

The Presenter is deliberately outside Master/Worker execution.  It receives only
the original goal, the public terminal result, and the replay verdict.  It cannot
access a browser, Worker APIs, or the private runtime data store.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.provider_config import build_chat_model
from llm.structured import (
    get_llm_call_count,
    get_llm_token_usage,
    invoke_structured,
)


_PRESENTER_SYSTEM = load_prompt_text("task.tool_agent.presentation")


class _PresentationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1)
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PresentationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["generated", "fallback"]
    reply: str
    result_digest: str
    model: str = ""
    elapsed_s: float = 0.0
    llm_calls: int = 0
    token_usage: dict[str, int] = Field(default_factory=dict)
    context_reports: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def result_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _display_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _record_table(rows: list[dict[str, Any]], *, chinese: bool) -> str:
    columns = list(dict.fromkeys(str(key) for row in rows for key in row))
    cell = lambda value: _display_value(value).replace("|", "\\|").replace("\n", "<br>")
    intro = f"查询到 {len(rows)} 条结果：" if chinese else f"Found {len(rows)} results:"
    header = "| " + " | ".join(column.replace("_", " ") for column in columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join(cell(row.get(column)) for column in columns) + " |"
        for row in rows
    ]
    return "\n\n".join((intro, "\n".join((header, separator, *body))))

def _deterministic_reply(
    *,
    goal: str,
    phase: str,
    result: Any,
    summary: str,
) -> str:
    if phase != "completed":
        return summary.strip() or "The task did not complete."
    if isinstance(result, str):
        return result.strip() or json.dumps(result, ensure_ascii=False)
    chinese = bool(re.search(r"[\u3400-\u9fff]", goal))
    if isinstance(result, dict):
        parts = [
            f"{str(key).replace('_', ' ')}{'为' if chinese else ' is '}{_display_value(value)}"
            for key, value in result.items()
        ]
        if chinese:
            return f"查询结果：{'，'.join(parts)}。"
        return f"Result: {', '.join(parts)}."
    if isinstance(result, (list, tuple)):
        rows = list(result)
        if not rows:
            return "未查询到结果。" if chinese else "No results found."
        if all(isinstance(value, dict) for value in rows):
            return _record_table(rows, chinese=chinese)
        values = ("、" if chinese else ", ").join(
            _display_value(value) for value in rows
        )
        return f"查询结果：{values}。" if chinese else f"Result: {values}."
    return f"结果为 {_display_value(result)}。" if chinese else f"The result is {_display_value(result)}."


def _salient_literals(value: Any, *, limit: int = 200) -> list[str]:
    values: list[str] = []

    def visit(item: Any) -> None:
        if len(values) >= limit:
            return
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
            return
        if item is None:
            return
        text = str(item).lower() if isinstance(item, bool) else str(item).strip()
        if text and text not in values:
            values.append(text)

    visit(value)
    return values


def _validate_fidelity(reply: str, result: Any) -> None:
    missing = [
        value
        for value in _salient_literals(result)
        if not _literal_is_preserved(reply, value)
    ]
    if missing:
        shown = ", ".join(repr(item) for item in missing[:5])
        raise ValueError(f"presentation omitted canonical result literal(s): {shown}")
    if (
        isinstance(result, (list, tuple))
        and result
        and all(isinstance(row, dict) for row in result)
        and not any(line.strip().startswith("|") for line in reply.splitlines())
    ):
        raise ValueError("presentation did not render record rows as a table")


def _literal_is_preserved(reply: str, value: str) -> bool:
    # URLs, emails, and mixed ASCII identifiers must remain byte-for-byte stable.
    if re.search(r"https?://|@", value) or (
        re.fullmatch(r"[A-Za-z0-9._:/-]+", value)
        and re.search(r"[A-Za-z]", value)
        and re.search(r"\d", value)
    ):
        return bool(re.search(
            rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
            reply,
        ))
    substitutions = {
        "小于等于": "<=",
        "不超过": "<=",
        "大于等于": ">=",
        "不少于": ">=",
        "小于": "<",
        "低于": "<",
        "大于": ">",
        "高于": ">",
    }
    comparable_reply = reply
    comparable_value = value
    for text, symbol in substitutions.items():
        comparable_reply = comparable_reply.replace(text, symbol)
        comparable_value = comparable_value.replace(text, symbol)
    tokens = re.findall(
        r"<=|>=|<|>|(?<![\d.])[-+]?\d+(?:\.\d+)?|[A-Za-z]+|[\u3400-\u9fff]+|℃|°|%",
        comparable_value,
    )
    if not tokens:
        return value in reply

    def token_is_preserved(token: str) -> bool:
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", token):
            # A bare substring check lets canonical 3 pass as corrupted 33/13.
            return bool(
                re.search(
                    rf"(?<![\d.]){re.escape(token)}(?![\d.])",
                    comparable_reply,
                )
            )
        if token in {"<", ">"}:
            return bool(
                re.search(
                    rf"(?<![<>]){re.escape(token)}(?![=])",
                    comparable_reply,
                )
            )
        if token in {"<=", ">="}:
            return token in comparable_reply
        if token.isascii() and token.isalpha():
            return bool(
                re.search(
                    rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])",
                    comparable_reply,
                )
            )
        return token in comparable_reply

    return all(token_is_preserved(token) for token in tokens)


def _validate_natural_reply(reply: str, result: Any) -> None:
    if not isinstance(result, (dict, list, tuple)):
        return
    try:
        parsed = json.loads(reply)
    except (TypeError, json.JSONDecodeError):
        return
    if isinstance(parsed, (dict, list)):
        raise ValueError("presentation reply is serialized structured data, not user-facing prose")


def _prompt_snapshot(messages: list[Any]) -> dict[str, Any]:
    roles = []
    for message in messages:
        role = "system" if isinstance(message, SystemMessage) else "human"
        text = str(getattr(message, "content", ""))
        roles.append({
            "role": role,
            "parts": [{
                "label": "instruction" if role == "system" else "presentation_input",
                "source_type": "runtime_message",
                "source": "tool_agent.presentation",
                "type": "text",
                "text": text,
                "chars": len(text),
            }],
        })
    return {
        "kind": "prompt_snapshot",
        "label": "tool_agent.presentation",
        "roles": roles,
    }


def present_result(
    *,
    goal: str,
    phase: str,
    result: Any,
    summary: str,
    replay: dict[str, Any] | None,
    llm: Any | None = None,
    model_name: str = "",
    invoke: Callable[..., _PresentationEnvelope] = invoke_structured,
) -> PresentationResult:
    """Render one public result without exposing execution capabilities."""
    digest = result_digest(result)
    replay_status = str((replay or {}).get("status") or "unavailable")
    fallback = _deterministic_reply(
        goal=goal,
        phase=phase,
        result=result,
        summary=summary,
    )
    if phase == "completed" and replay_status != "passed":
        return PresentationResult(
            status="fallback",
            reply=fallback,
            result_digest=digest,
            error=(
                "completed result was not sent to the Presenter because deterministic "
                f"replay status is {replay_status!r}"
            ),
        )
    payload = {
        "goal": goal,
        "execution": {
            "phase": phase,
            "summary": summary,
            "result": result,
            "result_digest": digest,
            "replay_status": replay_status,
            "must_preserve_literals": _salient_literals(result),
        },
    }
    messages = [
        SystemMessage(content=_PRESENTER_SYSTEM),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, indent=2, default=str)),
    ]
    reports = [_prompt_snapshot(messages)]
    started_at = time.perf_counter()
    calls_before = get_llm_call_count()
    input_before, output_before = get_llm_token_usage()
    resolved_model = model_name
    try:
        if llm is None:
            cfg = resolve_llm_config("tool_agent.presentation")
            resolved_model = cfg.model
            llm = build_chat_model(cfg)
        envelope = invoke(
            llm,
            messages,
            _PresentationEnvelope,
            trace_sink=reports,
            trace_label="tool_agent.presentation",
        )
        if envelope.result_digest != digest:
            raise ValueError("presentation result_digest does not match the execution result")
        reply = envelope.reply.strip()
        if phase == "completed":
            _validate_fidelity(reply, result)
            _validate_natural_reply(reply, result)
        status: Literal["generated", "fallback"] = "generated"
        error = ""
    except Exception as exc:  # noqa: BLE001 - presentation cannot change execution success
        reply = fallback
        status = "fallback"
        error = f"{type(exc).__name__}: {exc}"
    input_after, output_after = get_llm_token_usage()
    return PresentationResult(
        status=status,
        reply=reply,
        result_digest=digest,
        model=resolved_model,
        elapsed_s=round(time.perf_counter() - started_at, 3),
        llm_calls=max(0, get_llm_call_count() - calls_before),
        token_usage={
            "input": max(0, input_after - input_before),
            "output": max(0, output_after - output_before),
        },
        context_reports=reports,
        error=error,
    )


def write_presentation_artifact(
    run_dir: Path,
    presentation: PresentationResult,
) -> Path:
    path = run_dir / "tool_agent_presentation.json"
    path.write_text(presentation.model_dump_json(indent=2), encoding="utf-8")
    return path


__all__ = [
    "PresentationResult",
    "present_result",
    "result_digest",
    "write_presentation_artifact",
]
