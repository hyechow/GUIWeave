#!/usr/bin/env python
"""Run one action-policy case through structured and tool-call protocols.

Examples:
    uv run python manager_protocol/run.py
    uv run python manager_protocol/run.py --platform browser --case navigate-进入
    uv run python manager_protocol/run.py --variant tool_call_off --samples 1
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from gui_agent.core.config import resolve_llm_config  # noqa: E402
from manager_protocol.action_tools import (  # noqa: E402
    action_tools,
    decision_from_tool_call,
)

load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_SUITE = Path(__file__).with_name("suite.json")


class ProtocolError(ValueError):
    """One response did not satisfy the selected output protocol."""


@dataclass(frozen=True)
class PlatformRuntime:
    policy: Any
    decision_model: type[BaseModel]


def platform_runtime(platform: str) -> PlatformRuntime:
    if platform == "browser":
        from gui_agent.adapters.browser.policies import BrowserActionPolicy

        policy = BrowserActionPolicy()
    elif platform == "android":
        from gui_agent.adapters.android.policies import AndroidActionPolicy

        policy = AndroidActionPolicy()
    elif platform == "iphone":
        from gui_agent.adapters.iphone.policies.structured_output import StructuredOutputPolicy

        policy = StructuredOutputPolicy()
    else:
        raise ValueError(f"unsupported platform: {platform}")
    return PlatformRuntime(policy=policy, decision_model=policy.decision_schema)


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_source_case(
    suite: dict[str, Any], platform: str, selector: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = next(
        (item for item in suite["sources"] if item["platform"] == platform),
        None,
    )
    if source is None:
        raise ValueError(f"suite has no source for platform {platform!r}")
    cases = json.loads((PROJECT_ROOT / source["cases"]).read_text(encoding="utf-8"))
    if selector.isdigit():
        index = int(selector)
        try:
            return source, cases[index]
        except IndexError as exc:
            raise ValueError(f"case index {index} is outside 0..{len(cases) - 1}") from exc
    matches = [case for case in cases if selector.casefold() in case["label"].casefold()]
    if len(matches) != 1:
        raise ValueError(
            f"case selector {selector!r} matched {len(matches)} cases; use an index or unique label substring"
        )
    return source, matches[0]


def resolve_screenshot(source: dict[str, Any], case: dict[str, Any]) -> Path:
    declared = PROJECT_ROOT / case["screenshot"]
    if declared.is_file():
        return declared
    fallback = PROJECT_ROOT / source["screenshots"] / Path(case["screenshot"]).name
    if fallback.is_file():
        return fallback
    raise FileNotFoundError(
        f"screenshot not found at {declared} or {fallback}"
    )


def _schema_instruction(schema: type[BaseModel]) -> str:
    payload = schema.model_json_schema()
    return (
        "你必须只返回一个符合下方 schema 的业务 JSON 对象，不要使用 Markdown，"
        "不要输出解释或 JSON Schema 本身。\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _base_messages(
    runtime: PlatformRuntime,
    case: dict[str, Any],
    screenshot: bytes,
    *,
    protocol: str,
) -> list[Any]:
    hints = case.get("hints") or {}
    user_text = runtime.policy._build_user_text(
        case["instruction"],
        direction=hints.get("direction"),
        drag_column=hints.get("drag_column"),
        drag_steps=hints.get("drag_steps"),
    )
    system = runtime.policy.SYSTEM_PROMPT
    if protocol == "structured":
        system = f"{system}\n\n{_schema_instruction(runtime.decision_model)}"
    else:
        system = (
            f"{system}\n\n"
            "你必须通过调用恰好一个可用动作工具回答。工具名代表 action_type；"
            "不要输出普通文本，也不要同时调用多个工具。"
        )
    prepared = runtime.policy._prepare_png(screenshot)
    image = base64.b64encode(prepared).decode("ascii")
    return [
        SystemMessage(content=system),
        HumanMessage(content=[
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ]),
    ]


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("text")
        )
    return str(content or "")


def _parse_json_object(content: object) -> dict[str, Any]:
    text = _message_text(content).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    if start < 0:
        raise ProtocolError("structured response contains no JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as exc:
        raise ProtocolError(f"structured response contains invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("structured response is not a JSON object")
    return value


def _response_usage(response: Any) -> dict[str, Any]:
    usage = dict(getattr(response, "usage_metadata", None) or {})
    metadata = dict(getattr(response, "response_metadata", None) or {})
    token_usage = metadata.get("token_usage") or metadata.get("usage")
    if isinstance(token_usage, dict):
        usage.setdefault("provider_usage", token_usage)
    return usage


def _reasoning_content(response: Any) -> str:
    additional = getattr(response, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = additional.get(key)
        if value:
            return str(value)
    return ""


def _postprocess(runtime: PlatformRuntime, case: dict[str, Any], decision: BaseModel) -> BaseModel:
    hints = case.get("hints") or {}
    result = runtime.policy._postprocess(
        decision,
        case["instruction"],
        direction=hints.get("direction"),
        drag_column=hints.get("drag_column"),
        drag_steps=hints.get("drag_steps"),
    )
    return runtime.decision_model.model_validate(result.model_dump(mode="python"))


def score_action(action: Any, expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    meta = {"url_contains", "description_must_not_contain"}
    for key, wanted in expected.items():
        if key in meta:
            continue
        actual = getattr(action, key, None)
        if isinstance(wanted, list):
            if actual not in wanted:
                failures.append(f"{key}: expected one of {wanted!r}, got {actual!r}")
        elif actual != wanted:
            failures.append(f"{key}: expected {wanted!r}, got {actual!r}")
    if needle := expected.get("url_contains"):
        actual_url = str(getattr(action, "url", "") or "")
        if str(needle) not in actual_url:
            failures.append(f"url: expected to contain {needle!r}, got {actual_url!r}")
    description = str(getattr(action, "description", "") or "")
    for pattern in expected.get("description_must_not_contain", []):
        if re.search(pattern, description):
            failures.append(f"description must not match {pattern!r}")
    return failures


def run_variant(
    *,
    platform: str,
    runtime: PlatformRuntime,
    case: dict[str, Any],
    screenshot: bytes,
    variant: dict[str, Any],
    temperature: float,
) -> dict[str, Any]:
    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
        temperature=temperature,
    )
    protocol = variant["protocol"]
    messages = _base_messages(runtime, case, screenshot, protocol=protocol)
    thinking_body = {"enable_thinking": bool(variant["thinking"])}
    started = time.perf_counter()
    response: Any = None
    report: dict[str, Any] = {
        "variant": variant["id"],
        "protocol": protocol,
        "thinking": bool(variant["thinking"]),
        "provider": cfg.provider,
        "model": cfg.model,
        "protocol_valid": False,
        "args_valid": False,
        "action_type_correct": False,
        "ok": False,
    }
    if protocol == "tool_call":
        report["tool_choice"] = variant.get("tool_choice", "required")
    try:
        if protocol == "structured":
            response = llm.bind(
                response_format={"type": "json_object"},
                extra_body=thinking_body,
            ).invoke(messages)
            payload = _parse_json_object(response.content)
            decision = runtime.decision_model.model_validate(payload)
            report["raw_output"] = _message_text(response.content)
        elif protocol == "tool_call":
            tools = action_tools(platform)
            response = llm.bind_tools(
                [tool.spec() for tool in tools],
                tool_choice=variant.get("tool_choice", "required"),
                parallel_tool_calls=bool(variant.get("parallel_tool_calls", False)),
                extra_body=thinking_body,
            ).invoke(messages)
            calls = list(getattr(response, "tool_calls", None) or [])
            report["assistant_text"] = _message_text(response.content)
            report["tool_calls"] = calls
            if len(calls) != 1:
                raise ProtocolError(f"expected exactly one tool call, got {len(calls)}")
            call = calls[0]
            decision = decision_from_tool_call(
                platform,
                runtime.decision_model,
                str(call.get("name") or ""),
                dict(call.get("args") or {}),
            )
        else:
            raise ValueError(f"unknown protocol: {protocol}")

        report["protocol_valid"] = True
        report["args_valid"] = True
        decision = _postprocess(runtime, case, decision)
        action = getattr(decision, "action")
        failures = score_action(action, case["expected"])
        wanted_type = case["expected"].get("action_type")
        report["action_type_correct"] = (
            action.action_type in wanted_type
            if isinstance(wanted_type, list)
            else action.action_type == wanted_type
        )
        report["action"] = action.model_dump(mode="json", exclude_none=True)
        report["failures"] = failures
        report["ok"] = not failures
    except Exception as exc:  # noqa: BLE001 - each variant is an isolated sample
        report["error"] = f"{type(exc).__name__}: {exc}"
        report.setdefault("failures", [report["error"]])
    finally:
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        if response is not None:
            report["usage"] = _response_usage(response)
            reasoning = _reasoning_content(response)
            if reasoning:
                report["reasoning_content"] = reasoning
    return report


def _selected_variants(suite: dict[str, Any], selector: str) -> list[dict[str, Any]]:
    if selector == "all":
        return list(suite["variants"])
    variants = [item for item in suite["variants"] if item["id"] == selector]
    if not variants:
        raise ValueError(f"unknown variant {selector!r}")
    return variants


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--platform", choices=("iphone", "android", "browser"), default="browser")
    parser.add_argument("--case", default="0", help="zero-based index or unique label substring")
    parser.add_argument("--variant", default="all", help="variant id or all")
    parser.add_argument("--samples", type=int, default=1, help="samples per variant for this smoke run")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    suite = load_suite(args.suite)
    source, case = load_source_case(suite, args.platform, args.case)
    screenshot_path = resolve_screenshot(source, case)
    screenshot = screenshot_path.read_bytes()
    runtime = platform_runtime(args.platform)
    variants = _selected_variants(suite, args.variant)
    temperature = float(suite["sampling"].get("temperature", 0.0))

    print(f"case     : {case['label']}")
    print(f"platform : {args.platform}")
    print(f"image    : {screenshot_path.relative_to(PROJECT_ROOT)}")
    print(f"variants : {', '.join(item['id'] for item in variants)}")

    results: list[dict[str, Any]] = []
    for variant in variants:
        for sample in range(1, max(1, args.samples) + 1):
            print(f"[{variant['id']}] sample {sample}/{max(1, args.samples)} ...", flush=True)
            result = run_variant(
                platform=args.platform,
                runtime=runtime,
                case=case,
                screenshot=screenshot,
                variant=variant,
                temperature=temperature,
            )
            result["sample"] = sample
            results.append(result)
            status = "PASS" if result["ok"] else "FAIL"
            detail = result.get("error") or "; ".join(result.get("failures") or [])
            print(f"  {status} {result.get('action', {}).get('action_type', '-')} {detail}")

    payload = {
        "experiment": suite["name"],
        "case": {
            "platform": args.platform,
            "label": case["label"],
            "instruction": case["instruction"],
            "expected": case["expected"],
            "screenshot": str(screenshot_path.relative_to(PROJECT_ROOT)),
        },
        "results": results,
        "summary": {
            "passed": sum(1 for item in results if item["ok"]),
            "total": len(results),
        },
    }
    if not args.no_write:
        output_dir = PROJECT_ROOT / suite["output_root"] / time.strftime("%Y%m%d_%H%M%S")
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "report.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report   : {output.relative_to(PROJECT_ROOT)}")
    return 0 if payload["summary"]["passed"] == payload["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
