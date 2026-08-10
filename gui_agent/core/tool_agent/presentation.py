"""User-facing presentation for verified Tool Agent execution results.

The Presenter is deliberately outside Master/Worker execution.  It receives only
the original goal, the public terminal result, and the replay verdict.  It cannot
access a browser, Worker APIs, or the private runtime data store.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
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


def _deterministic_reply(*, phase: str, result: Any, summary: str) -> str:
    if phase != "completed":
        return summary.strip() or "The task did not complete."
    if isinstance(result, str):
        return result.strip() or json.dumps(result, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


def _salient_literals(value: Any, *, limit: int = 20) -> list[str]:
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
        if item is None or isinstance(item, bool):
            return
        text = str(item).strip()
        if text and text not in values:
            values.append(text)

    visit(value)
    return values


def _validate_fidelity(reply: str, result: Any) -> None:
    missing = [value for value in _salient_literals(result) if value not in reply]
    if missing:
        shown = ", ".join(repr(item) for item in missing[:5])
        raise ValueError(f"presentation omitted canonical result literal(s): {shown}")


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
    fallback = _deterministic_reply(phase=phase, result=result, summary=summary)
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
            llm = ChatOpenAI(
                model=cfg.model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                timeout=cfg.timeout_s,
                max_retries=cfg.max_retries,
            )
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
