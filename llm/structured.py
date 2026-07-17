"""Structured-output helpers for OpenAI-compatible chat providers."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, MutableSequence
from typing import Any, TypeVar

from json_repair import loads as _repair_json_loads
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import BadRequestError
from pydantic import BaseModel, ValidationError

from llm.provider_config import dashscope_extra_body

ModelT = TypeVar("ModelT", bound=BaseModel)
ReturnT = TypeVar("ReturnT")
_LLM_CALL_COUNT = 0
_LLM_INPUT_TOKENS = 0
_LLM_OUTPUT_TOKENS = 0
MAX_LLM_TRANSIENT_RETRIES = 2


def _llm_model_name(llm: ChatOpenAI) -> str | None:
    """Best-effort model id from a ChatOpenAI instance (varies by langchain version)."""
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return None


class StructuredOutputError(ValueError):
    """One model response could not be validated as the requested result."""


def get_llm_call_count() -> int:
    """Return the number of LLM API calls made through invoke_structured."""
    return _LLM_CALL_COUNT


def get_llm_token_usage() -> tuple[int, int]:
    """Return cumulative (input_tokens, output_tokens) over all invoke_structured calls.

    Same scope/caveats as get_llm_call_count: only counts calls routed through
    invoke_structured; transient-retry attempts and concurrent (e.g. target_verify
    pool) calls accumulate into the same globals. Providers that omit usage report 0.
    """
    return _LLM_INPUT_TOKENS, _LLM_OUTPUT_TOKENS


def _accumulate_usage(response: object) -> None:
    """Add one response's token usage to the global counters (best-effort)."""
    global _LLM_INPUT_TOKENS, _LLM_OUTPUT_TOKENS
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        _LLM_INPUT_TOKENS += int(usage.get("input_tokens") or 0)
        _LLM_OUTPUT_TOKENS += int(usage.get("output_tokens") or 0)
        return
    # Fallback: OpenAI-style response_metadata.token_usage (prompt/completion)
    meta = getattr(response, "response_metadata", None) or {}
    tu = meta.get("token_usage") or meta.get("usage") or {}
    if isinstance(tu, dict):
        _LLM_INPUT_TOKENS += int(tu.get("prompt_tokens") or 0)
        _LLM_OUTPUT_TOKENS += int(tu.get("completion_tokens") or 0)


def invoke_structured(
    llm: ChatOpenAI,
    messages: list[BaseMessage],
    schema: type[ModelT],
    *,
    trace_sink: MutableSequence[dict] | Callable[[dict], None] | None = None,
    trace_label: str = "",
    fallback_on_invalid: bool = True,
) -> ModelT:
    """Invoke a chat model and parse a Pydantic object.

    Uses DashScope json_object constrained decoding. Thinking is off by default for
    latency, but models that require enable_thinking=True (e.g. qwen3.7-*) keep it on.
    Falls back to plain JSON text parsing if the constrained mode fails.
    """
    instruction = _json_schema_instruction(schema)
    msgs = _with_json_instruction(messages, schema, instruction=instruction)
    _append_schema_instruction_trace(trace_sink, trace_label or schema.__name__, instruction)

    model_name = _llm_model_name(llm)
    thinking_body = dashscope_extra_body(model_name)
    # Primary: json_object mode (constrained decoding) + model-aware thinking flag
    bound = llm.bind(
        response_format={"type": "json_object"},
        extra_body=thinking_body,
    )
    primary_error: Exception | None = None
    try:
        response = _invoke_counted_with_retry(
            lambda: bound.invoke(msgs),
            label="json_object",
        )
        content = _message_text(response.content)
        parsed = _parse_structured_response(content, schema)
        _append_trace(
            trace_sink,
            label=trace_label,
            schema=schema,
            mode="json_object",
            raw_output=content,
            parsed=parsed,
        )
        return parsed
    except (BadRequestError, ValidationError, ValueError) as exc:
        primary_error = exc
        if not fallback_on_invalid:
            raise StructuredOutputError(
                f"{trace_label or schema.__name__} structured output is invalid: {exc}"
            ) from exc
        print(f"json_object 模式失败（{type(exc).__name__}）: {exc}，改用纯文本 JSON 解析...")

    # Fallback: plain text, let model output JSON freely (retry once on parse failure).
    # Re-bind enable_thinking so a constructor default of False cannot 400 on forced-thinking models.
    fallback_llm = llm.bind(extra_body=thinking_body)
    fallback_msgs = _with_repair_instruction(msgs, schema, primary_error)
    for fallback_attempt in range(2):
        response = _invoke_counted_with_retry(
            lambda: fallback_llm.invoke(fallback_msgs),
            label="json text fallback",
        )
        content = _message_text(response.content)
        try:
            parsed = _parse_structured_response(content, schema)
            _append_trace(
                trace_sink,
                label=trace_label,
                schema=schema,
                mode="json_text_fallback",
                raw_output=content,
                parsed=parsed,
                attempt=fallback_attempt + 1,
            )
            return parsed
        except (ValidationError, ValueError) as exc:
            if fallback_attempt == 0:
                print(f"  fallback 解析失败，重试一次...")
                fallback_msgs = _with_repair_instruction(fallback_msgs, schema, exc, content)
                continue
            raise ValueError(
                f"结构化输出解析失败（primary + fallback 均失败）: {exc}\n"
                f"模型原始输出: {content[:500]}"
            ) from exc


def _append_trace(
    sink: MutableSequence[dict] | Callable[[dict], None] | None,
    *,
    label: str,
    schema: type[BaseModel],
    mode: str,
    raw_output: str,
    parsed: BaseModel,
    attempt: int | None = None,
) -> None:
    if sink is None:
        return
    report: dict[str, Any] = {
        "kind": "llm_output",
        "label": label or schema.__name__,
        "schema": schema.__name__,
        "mode": mode,
        "raw_output": raw_output,
        "parsed": parsed.model_dump(exclude_none=True),
        "chars": len(raw_output),
    }
    if attempt is not None:
        report["attempt"] = attempt
    if callable(sink):
        sink(report)
    else:
        sink.append(report)


def _append_schema_instruction_trace(
    sink: MutableSequence[dict] | Callable[[dict], None] | None,
    label: str,
    instruction: str,
) -> None:
    if sink is None or callable(sink):
        return
    for report in reversed(sink):
        if (
            isinstance(report, dict)
            and report.get("kind") == "prompt_snapshot"
            and report.get("label") == label
        ):
            roles = report.get("roles") or []
            system = next(
                (role for role in roles if isinstance(role, dict) and role.get("role") == "system"),
                None,
            )
            if not isinstance(system, dict):
                return
            parts = system.setdefault("parts", [])
            if any(isinstance(part, dict) and part.get("label") == "schema_instruction" for part in parts):
                return
            parts.append({
                "label": "schema_instruction",
                "source_type": "structured_output",
                "source": "invoke_structured",
                "type": "text",
                "text": instruction,
                "chars": len(instruction),
            })
            return


def _invoke_counted_with_retry(fn: Callable[[], ReturnT], label: str) -> ReturnT:
    """Invoke an LLM call, counting attempts and retrying transient bad payloads."""

    global _LLM_CALL_COUNT
    for attempt in range(MAX_LLM_TRANSIENT_RETRIES + 1):
        _LLM_CALL_COUNT += 1
        try:
            result = fn()
            _accumulate_usage(result)
            return result
        except TypeError as exc:
            if not _is_transient_response_error(exc) or attempt >= MAX_LLM_TRANSIENT_RETRIES:
                raise
            wait_s = 0.5 * (attempt + 1)
            print(f"LLM {label} 响应格式异常，{wait_s:.1f}s 后重试...")
            time.sleep(wait_s)
    raise RuntimeError("unreachable")


def _is_transient_response_error(exc: TypeError) -> bool:
    text = str(exc)
    return (
        "null value for 'choices'" in text
        or "Received response with null value for 'choices'" in text
    )


def _with_json_instruction(
    messages: list[BaseMessage],
    schema: type[BaseModel],
    *,
    instruction: str | None = None,
) -> list[BaseMessage]:
    """Merge JSON schema instruction into the system message (or prepend one)."""
    instruction = instruction or _json_schema_instruction(schema)
    if messages and isinstance(messages[0], SystemMessage):
        merged = SystemMessage(content=f"{messages[0].content}\n\n{instruction}")
        return [merged, *messages[1:]]
    return [SystemMessage(content=instruction), *messages]


def _json_schema_instruction(schema: type[BaseModel]) -> str:
    """Instruction appended to structured-output prompts."""
    schema_json = schema.model_json_schema()
    properties = schema_json.get("properties", {})
    required = schema_json.get("required", [])
    optional = [key for key in properties if key not in required]
    required_text = ", ".join(required) or "无"
    optional_text = ", ".join(optional) or "无"
    instruction = (
        "你必须只返回一个 JSON 对象，不要使用 Markdown，不要输出额外说明。\n"
        "返回的是业务结果实例，不是 JSON Schema。禁止返回 type/properties/description/required 这类 schema 字段作为顶层对象。\n"
        f"顶层必填字段：{required_text}\n"
        f"顶层可选字段：{optional_text}\n"
        "JSON 必须符合以下 schema（仅作为格式约束，不要照抄它）：\n"
        f"{json.dumps(schema_json, ensure_ascii=False)}"
    )
    return instruction


def _with_repair_instruction(
    messages: list[BaseMessage],
    schema: type[BaseModel],
    error: Exception | None,
    raw_content: str | None = None,
) -> list[BaseMessage]:
    """Append a correction prompt after malformed structured output."""
    schema_json = schema.model_json_schema()
    required = schema_json.get("required", [])
    properties = list(schema_json.get("properties", {}).keys())
    detail = f"\n上一次错误：{error}" if error else ""
    raw = f"\n上一次原始输出片段：{raw_content[:300]}" if raw_content else ""
    instruction = (
        "请重新输出一个业务结果 JSON 对象。"
        f"顶层字段只能来自：{', '.join(properties)}。"
        f"必须包含：{', '.join(required) or '无'}。"
        "不要返回 JSON Schema，不要把 description/properties/type/required 作为顶层字段。"
        f"{detail}{raw}"
    )
    return [*messages, HumanMessage(content=instruction)]


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


def _parse_structured_response(text: str, schema: type[ModelT]) -> ModelT:
    repaired = False
    try:
        data: object = json.loads(_extract_json_object(text))
    except ValueError:
        # 高频结构化失败:任务文案含字面双引号(如描述要设为 "3 customer(s) love it!"),模型在
        # JSON 字符串字段里转义不干净 → json.loads 直接崩、decompose 抛异常、整个 run traceback 退出
        # (webarena 544)。json_repair 是确定性修复器,对未转义内层引号/尾随逗号/轻度截断尽力恢复,
        # 把"必崩"变"尽力恢复"。修复结果可能有轻微失真,所以只在严格解析失败时才走此兜底。
        data = _repair_json_object(text)
        if data is None:
            raise
        repaired = True
    if _looks_like_schema_echo(data, schema):
        raise ValueError("模型返回了 JSON Schema，而不是业务结果对象")
    parsed = schema.model_validate(data)
    if repaired:
        print("  json.loads 失败，json_repair 恢复并通过 schema 校验")
    return parsed


def _looks_like_schema_echo(data: object, schema: type[BaseModel]) -> bool:
    if not isinstance(data, dict):
        return False
    keys = set(data)
    if {"type", "properties"}.issubset(keys):
        schema_fields = set(schema.model_json_schema().get("properties", {}))
        echoed_fields = set(data.get("properties") or {})
        return not schema_fields.isdisjoint(echoed_fields)
    return False


def _strip_code_fence(text: str) -> str:
    """Drop a leading ```/```json fence and its closing ``` so the JSON body is bare."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _extract_json_object(text: str) -> str:
    stripped = _strip_code_fence(text)
    decoder = json.JSONDecoder()
    start = stripped.find("{")
    if start < 0:
        raise ValueError(f"LLM response did not contain a JSON object: {text}")
    _, end = decoder.raw_decode(stripped[start:])
    return stripped[start : start + end]


def _repair_json_object(text: str) -> dict | None:
    """Deterministically salvage a malformed LLM JSON object → parsed dict, or None if
    nothing recoverable. Only called after strict json.loads has already failed. Rejects a
    non-dict or empty result so a hopeless payload still surfaces the original parse error
    (an empty {} would otherwise be silently validated into a defaults-only object)."""
    stripped = _strip_code_fence(text)
    start = stripped.find("{")
    if start < 0:
        return None
    try:
        repaired = _repair_json_loads(stripped[start:])
    except Exception:
        return None
    return repaired if isinstance(repaired, dict) and repaired else None
