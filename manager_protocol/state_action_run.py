#!/usr/bin/env python
"""Replay passed WebArena frames to compare state/action architectures.

The replay is deliberately offline: it reads screenshots and decisions from a
completed WebArena run, asks the model what it would do on that frozen frame,
and never dispatches the predicted action to ``ctx.api``.

Examples:
    uv run python manager_protocol/state_action_run.py --no-write
    uv run python manager_protocol/state_action_run.py --case dashboard_open_sales
    uv run python manager_protocol/state_action_run.py --variant joint_content_tool
    uv run python manager_protocol/state_action_run.py --thinking off --samples 3
    uv run python manager_protocol/state_action_run.py --thinking off --tool-choice required
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

from gui_agent.core.config import resolve_llm_config  # noqa: E402
from manager_protocol.action_tools import action_tools, decision_from_tool_call  # noqa: E402
from manager_protocol.run import (  # noqa: E402
    PlatformRuntime,
    ProtocolError,
    _message_text,
    _parse_json_object,
    _reasoning_content,
    _response_usage,
    platform_runtime,
)

load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_SUITE = Path(__file__).with_name("state_action_suite.json")


class StateAssessment(BaseModel):
    """Small state-machine boundary used by the replay experiment."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["in_progress", "satisfied", "blocked"]
    summary: str = Field(min_length=1)
    established_facts: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)
    next_instruction: str = Field(
        default="",
        description="status=in_progress 时，下一条原子 GUI 操作指令",
    )
    target_control: str = ""
    target_value: str = ""
    action_family: Literal["activate", "input", "select", "iterate", "navigate", ""] = ""
    expected_result: str = ""


@dataclass(frozen=True)
class ReplayCase:
    spec: dict[str, Any]
    task_goal: str
    contract_goal: str
    contract_success: str
    decision_goal: str
    decision_success: str
    recorded_summary: str
    recorded_instruction: str
    recorded_expected_result: str
    source_run: str
    screenshot_path: Path
    screenshot: bytes


def load_suite(path: Path = DEFAULT_SUITE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _official_score(run_dir: Path) -> float | None:
    stdout = run_dir / "stdout.log"
    if not stdout.is_file():
        return None
    text = stdout.read_text(encoding="utf-8", errors="replace")
    marker = '"score": 1.0'
    if marker in text or "status=success, score=1.0" in text:
        return 1.0
    return None


def load_replay_case(suite: dict[str, Any], spec: dict[str, Any]) -> ReplayCase:
    source_run = str(spec.get("source_run") or suite["source_run"])
    run_dir = PROJECT_ROOT / source_run
    context_path = run_dir / "context.json"
    if not context_path.is_file():
        raise FileNotFoundError(f"WebArena context is missing: {context_path}")
    required_score = suite.get("require_official_score")
    if required_score is not None and _official_score(run_dir) != float(required_score):
        raise ValueError(
            f"source run does not contain official score={required_score}: {run_dir}"
        )

    context = json.loads(context_path.read_text(encoding="utf-8"))
    event_index = int(spec["event_index"])
    event = next(
        (
            item
            for item in context.get("journal", {}).get("events", [])
            if item.get("event_type") == "turn" and item.get("index") == event_index
        ),
        None,
    )
    if event is None:
        raise ValueError(f"turn {event_index} is absent from {context_path}")

    observation_url = str(event.get("observation_url") or f"screenshot_turn_{event_index}.png")
    screenshot_path = run_dir / observation_url
    if not screenshot_path.is_file():
        raise FileNotFoundError(f"WebArena screenshot is missing: {screenshot_path}")

    runtime_contract = event.get("runtime_state", {}).get("contract") or {}
    statement = event.get("statement") or {}
    supervisor = event.get("supervisor") or {}
    action_intent = supervisor.get("action_intent") or {}
    return ReplayCase(
        spec=spec,
        task_goal=str(context.get("goal") or ""),
        contract_goal=str(runtime_contract.get("goal") or statement.get("goal") or ""),
        contract_success=str(
            runtime_contract.get("success") or statement.get("success") or ""
        ),
        decision_goal=str(
            spec.get("decision_goal")
            or runtime_contract.get("goal")
            or statement.get("goal")
            or ""
        ),
        decision_success=str(
            spec.get("decision_success")
            or runtime_contract.get("success")
            or statement.get("success")
            or ""
        ),
        recorded_summary=str(supervisor.get("summary") or ""),
        recorded_instruction=str(action_intent.get("instruction") or ""),
        recorded_expected_result=str(action_intent.get("expected_result") or ""),
        source_run=source_run,
        screenshot_path=screenshot_path,
        screenshot=screenshot_path.read_bytes(),
    )


def _common_frame_text(case: ReplayCase) -> str:
    return (
        f"总体任务：{case.task_goal}\n"
        f"当前决策目标：{case.decision_goal}\n"
        f"当前决策完成条件：{case.decision_success}\n\n"
        "请只根据这些任务约束和当前截图判断当前状态及下一步。"
    )


def _image_message(text: str, screenshot: bytes) -> HumanMessage:
    image = base64.b64encode(screenshot).decode("ascii")
    return HumanMessage(
        content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ]
    )


def _state_schema_instruction() -> str:
    schema = json.dumps(StateAssessment.model_json_schema(), ensure_ascii=False)
    return (
        "只返回一个符合下方 schema 的 JSON 对象，不要使用 Markdown。"
        "如果 status=in_progress，next_instruction 必须是一条原子 GUI 操作指令；"
        "不要在状态输出中预测坐标。"
        "填写或替换文字时 action_family=input、target_value=完整文字，"
        "next_instruction 必须要求直接输入，禁止拆成先点击聚焦。"
        "设置下拉选项时 action_family=select、target_value=选项完整文字，"
        "next_instruction 必须要求直接选择该选项，禁止拆成先点击展开。"
        "滚动时 action_family=iterate；普通点击时 action_family=activate。\n"
        f"{schema}"
    )


BASE_POLICY_PROMPT = """你是一个平台无关的 GUI 决策器。
根据任务约束和当前截图选择恰好一个最小、原子的下一步动作。
不得假设截图中没有显示的状态；不得把多个动作合并成一次调用。
动作坐标使用 0-1000 归一化坐标系。"""


def _action_system_prompt(runtime: PlatformRuntime, output_contract: str) -> str:
    return (
        f"{runtime.policy.SYSTEM_PROMPT}\n\n"
        "本实验中的用户消息给出当前决策目标，而不一定已经给出具体动作。"
        "你需要先依据截图选择一个最小的下一步，再按动作工具落点。"
        "所有 x/y 参数都必须各自是单个数值，禁止用数组、区间或 bounding box。\n\n"
        f"{output_contract}"
    )


def _make_llm(temperature: float) -> tuple[ChatOpenAI, Any]:
    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout_s,
        max_retries=cfg.max_retries,
        temperature=temperature,
    )
    return llm, cfg


def _phase_report(response: Any, elapsed: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "elapsed_seconds": round(elapsed, 3),
        "usage": _response_usage(response),
    }
    text = _message_text(response.content).strip()
    if text:
        result["assistant_text"] = text
    calls = list(getattr(response, "tool_calls", None) or [])
    if calls:
        result["tool_calls"] = calls
    reasoning = _reasoning_content(response)
    if reasoning:
        result["reasoning_content"] = reasoning
    return result


def _invoke_action(
    *,
    llm: ChatOpenAI,
    runtime: PlatformRuntime,
    system_prompt: str,
    user_text: str,
    screenshot: bytes,
    thinking: bool,
    tool_choice: Literal["auto", "required"],
) -> tuple[Any, dict[str, Any]]:
    started = time.perf_counter()
    response = llm.bind_tools(
        [tool.spec() for tool in action_tools("browser")],
        tool_choice=tool_choice,
        parallel_tool_calls=False,
        extra_body={"enable_thinking": thinking},
    ).invoke([
        SystemMessage(content=system_prompt),
        _image_message(user_text, screenshot),
    ])
    phase = _phase_report(response, time.perf_counter() - started)
    return response, phase


def _decision_from_response(response: Any, runtime: PlatformRuntime) -> BaseModel:
    calls = list(getattr(response, "tool_calls", None) or [])
    if len(calls) != 1:
        raise ProtocolError(f"expected exactly one action tool call, got {len(calls)}")
    call = calls[0]
    decision = decision_from_tool_call(
        "browser",
        runtime.decision_model,
        str(call.get("name") or ""),
        dict(call.get("args") or {}),
    )
    return decision


def _invoke_state(
    *,
    llm: ChatOpenAI,
    case: ReplayCase,
    thinking: bool,
) -> tuple[StateAssessment, dict[str, Any]]:
    started = time.perf_counter()
    response = llm.bind(
        response_format={"type": "json_object"},
        extra_body={"enable_thinking": thinking},
    ).invoke([
        SystemMessage(content=f"{BASE_POLICY_PROMPT}\n\n{_state_schema_instruction()}"),
        _image_message(_common_frame_text(case), case.screenshot),
    ])
    phase = _phase_report(response, time.perf_counter() - started)
    state = StateAssessment.model_validate(_parse_json_object(response.content))
    return state, phase


def score_state(state: StateAssessment, expected: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(state.model_dump(mode="json"), ensure_ascii=False).casefold()
    missing = [
        str(keyword)
        for keyword in expected.get("keywords", [])
        if str(keyword).casefold() not in rendered
    ]
    status_correct = state.status == expected.get("status")
    return {
        "status_correct": status_correct,
        "missing_keywords": missing,
        "ok": status_correct and not missing,
    }


def score_action(action: Any, expected: dict[str, Any]) -> dict[str, Any]:
    action_type_correct = action.action_type == expected["action_type"]
    actual_fields = action.model_dump(mode="python")
    field_failures = _field_failures(actual_fields, expected)
    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    target_hit: bool | None = None
    if "target_box" in expected:
        target_box = [float(value) for value in expected["target_box"]]
        target_hit = (
            x is not None
            and y is not None
            and target_box[0] <= float(x) <= target_box[1]
            and target_box[2] <= float(y) <= target_box[3]
        )
    distance = None
    if "point" in expected and x is not None and y is not None:
        point = [float(value) for value in expected["point"]]
        distance = round(math.hypot(float(x) - point[0], float(y) - point[1]), 3)
    return {
        "action_type_correct": action_type_correct,
        "target_hit": target_hit,
        "field_failures": field_failures,
        "fields_correct": not field_failures,
        "distance_to_recorded_point": distance,
        "ok": action_type_correct and not field_failures and target_hit is not False,
    }


ACTION_TOOL_NAMES = {
    "type": "type_text",
    "upload": "upload_file",
}


def _field_failures(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key, wanted in (expected.get("fields") or {}).items():
        value = actual.get(key)
        if value != wanted:
            failures.append(f"{key}: expected {wanted!r}, got {value!r}")
    for key, wanted in (expected.get("fields_casefold") or {}).items():
        value = actual.get(key)
        if str(value or "").casefold() != str(wanted).casefold():
            failures.append(f"{key}: expected case-insensitive {wanted!r}, got {value!r}")
    if needle := expected.get("url_contains"):
        value = str(actual.get("url") or "")
        if str(needle) not in value:
            failures.append(f"url: expected to contain {needle!r}, got {value!r}")
    return failures


def diagnose_tool_attempt(
    tool_calls: list[dict[str, Any]], expected: dict[str, Any]
) -> dict[str, Any]:
    """Inspect malformed coordinates without treating them as protocol-valid.

    Qwen occasionally puts a point pair into a scalar ``x`` or ``y`` argument.
    Candidate recovery is diagnostic only: it tells us whether the model located
    the right target even though the runtime must still reject the tool call.
    """

    expected_name = str(
        expected.get("tool_name")
        or ACTION_TOOL_NAMES.get(str(expected["action_type"]), expected["action_type"])
    )
    name_correct = len(tool_calls) == 1 and tool_calls[0].get("name") == expected_name
    candidates: set[tuple[float, float]] = set()
    if len(tool_calls) == 1:
        args = tool_calls[0].get("args") or {}
        raw_x, raw_y = args.get("x"), args.get("y")

        def decoded(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except ValueError:
                    return value
            return value

        raw_x, raw_y = decoded(raw_x), decoded(raw_y)

        def number(value: Any) -> float | None:
            return float(value) if isinstance(value, (int, float)) else None

        x_number, y_number = number(raw_x), number(raw_y)
        if x_number is not None and y_number is not None:
            candidates.add((x_number, y_number))
        for value in (raw_x, raw_y):
            if (
                isinstance(value, list)
                and len(value) == 2
                and number(value[0]) is not None
                and number(value[1]) is not None
            ):
                candidates.add((float(value[0]), float(value[1])))
        if isinstance(raw_x, list) and isinstance(raw_y, list):
            for index in (0, -1):
                if number(raw_x[index]) is not None and number(raw_y[index]) is not None:
                    candidates.add((float(raw_x[index]), float(raw_y[index])))

    target_hit: bool | None = None
    if "target_box" in expected:
        box = [float(value) for value in expected["target_box"]]
        target_hit = any(
            box[0] <= x <= box[1] and box[2] <= y <= box[3]
            for x, y in candidates
        )
    raw_args = (
        dict(tool_calls[0].get("args") or {})
        if len(tool_calls) == 1
        else {}
    )
    field_failures = _field_failures(raw_args, expected)
    return {
        "tool_name_correct": name_correct,
        "coordinate_candidates": [list(point) for point in sorted(candidates)],
        "diagnostic_target_hit": target_hit,
        "field_failures": field_failures,
        "diagnostic_action_match": (
            name_correct and not field_failures and target_hit is not False
        ),
    }


def run_variant(
    *,
    case: ReplayCase,
    runtime: PlatformRuntime,
    variant: str,
    thinking: bool,
    tool_choice: Literal["auto", "required"],
    temperature: float,
) -> dict[str, Any]:
    llm, cfg = _make_llm(temperature)
    report: dict[str, Any] = {
        "variant": variant,
        "thinking": thinking,
        "tool_choice": tool_choice,
        "provider": cfg.provider,
        "model": cfg.model,
        "protocol_valid": False,
        "args_valid": False,
        "ok": False,
        "phases": [],
    }
    state: StateAssessment | None = None
    decision: BaseModel | None = None
    started = time.perf_counter()
    try:
        if variant == "action_only":
            response, phase = _invoke_action(
                llm=llm,
                runtime=runtime,
                system_prompt=_action_system_prompt(
                    runtime,
                    "只通过调用恰好一个动作工具回答，不要输出普通文本。",
                ),
                user_text=_common_frame_text(case),
                screenshot=case.screenshot,
                thinking=thinking,
                tool_choice=tool_choice,
            )
            report["phases"].append({"name": "action", **phase})
            decision = _decision_from_response(response, runtime)
        elif variant == "joint_content_tool":
            response, phase = _invoke_action(
                llm=llm,
                runtime=runtime,
                system_prompt=_action_system_prompt(
                    runtime,
                    "在同一个 assistant 响应中同时完成两件事："
                    "content 必须是状态 JSON，tool_calls 必须包含恰好一个下一步动作。\n"
                    f"{_state_schema_instruction()}",
                ),
                user_text=_common_frame_text(case),
                screenshot=case.screenshot,
                thinking=thinking,
                tool_choice=tool_choice,
            )
            report["phases"].append({"name": "joint", **phase})
            state = StateAssessment.model_validate(_parse_json_object(response.content))
            decision = _decision_from_response(response, runtime)
        elif variant == "separate_two_call":
            state, phase = _invoke_state(llm=llm, case=case, thinking=thinking)
            report["phases"].append({"name": "state", **phase})
            if state.status != "in_progress" or not state.next_instruction.strip():
                raise ProtocolError(
                    "state phase did not produce an in-progress next_instruction"
                )
            action_frame = (
                f"状态机输出：{json.dumps(state.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                f"动作指令：{state.next_instruction}\n"
                f"语义操作：{state.action_family}\n"
                f"目标控件：{state.target_control}\n"
                f"目标值：{state.target_value}\n"
                f"预期可见结果：{state.expected_result}\n"
                "请在当前截图中定位目标，只调用一个动作工具。"
            )
            response, phase = _invoke_action(
                llm=llm,
                runtime=runtime,
                system_prompt=_action_system_prompt(
                    runtime,
                    "状态判断已经由上游状态机完成。你只负责把给定动作指令落成"
                    "恰好一个物理动作工具，不要重新规划任务。",
                ),
                user_text=action_frame,
                screenshot=case.screenshot,
                thinking=thinking,
                tool_choice=tool_choice,
            )
            report["phases"].append({"name": "action", **phase})
            decision = _decision_from_response(response, runtime)
        elif variant == "recorded_intent_policy":
            action_frame = (
                f"已记录状态：{case.recorded_summary}\n"
                f"动作指令：{case.recorded_instruction}\n"
                f"预期结果：{case.recorded_expected_result}\n\n"
                "请在当前截图中定位目标，只调用一个动作工具。"
            )
            response, phase = _invoke_action(
                llm=llm,
                runtime=runtime,
                system_prompt=_action_system_prompt(
                    runtime,
                    "状态判断已经由上游状态机完成。你只负责把给定动作指令落成"
                    "恰好一个物理动作工具，不要重新规划任务。",
                ),
                user_text=action_frame,
                screenshot=case.screenshot,
                thinking=thinking,
                tool_choice=tool_choice,
            )
            report["phases"].append({"name": "action", **phase})
            decision = _decision_from_response(response, runtime)
        else:
            raise ValueError(f"unknown variant: {variant}")

        report["protocol_valid"] = True
        report["args_valid"] = True
        if state is not None:
            report["state"] = state.model_dump(mode="json")
            report["state_score"] = score_state(state, case.spec["expected_state"])
        assert decision is not None
        action = getattr(decision, "action")
        report["action"] = action.model_dump(mode="json", exclude_none=True)
        report["action_score"] = score_action(action, case.spec["expected_action"])
        report["ok"] = report["action_score"]["ok"] and (
            state is None or report["state_score"]["ok"]
        )
    except Exception as exc:  # noqa: BLE001 - variants are isolated observations
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if state is not None and "state" not in report:
            report["state"] = state.model_dump(mode="json")
            report["state_score"] = score_state(state, case.spec["expected_state"])
        tool_calls = [
            call
            for phase in report["phases"]
            for call in phase.get("tool_calls", [])
        ]
        report["tool_call_count"] = len(tool_calls)
        report["content_present"] = any(
            bool(str(phase.get("assistant_text") or "").strip())
            for phase in report["phases"]
        )
        report["missing_tool"] = not tool_calls
        report["missing_state_content"] = (
            variant == "joint_content_tool" and not report["content_present"]
        )
        report["attempt_diagnostic"] = diagnose_tool_attempt(
            tool_calls, case.spec["expected_action"]
        )
        report["tool_name_correct"] = report["attempt_diagnostic"]["tool_name_correct"]
        strict_match = bool((report.get("action_score") or {}).get("ok"))
        report["semantic_action_match"] = (
            strict_match
            or bool(report["attempt_diagnostic"].get("diagnostic_action_match"))
        )
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return report


def _select(items: list[Any], selector: str, key: Any) -> list[Any]:
    if selector == "all":
        return items
    selected = [item for item in items if key(item) == selector]
    if not selected:
        raise ValueError(f"unknown selector: {selector}")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--case", default="all", help="case id or all")
    parser.add_argument("--variant", default="all", help="variant id or all")
    parser.add_argument("--samples", type=int, default=None)
    parser.add_argument("--thinking", choices=("on", "off"), default=None)
    parser.add_argument("--tool-choice", choices=("auto", "required"), default=None)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    suite = load_suite(args.suite)
    case_specs = _select(suite["cases"], args.case, lambda item: item["id"])
    variants = _select(suite["variants"], args.variant, lambda item: item)
    sampling = suite.get("sampling") or {}
    samples = max(1, args.samples or int(sampling.get("samples_per_case", 1)))
    thinking = (
        args.thinking == "on"
        if args.thinking is not None
        else bool(sampling.get("thinking", True))
    )
    tool_choice = args.tool_choice or str(sampling.get("tool_choice", "auto"))
    temperature = float(sampling.get("temperature", 0.0))
    runtime = platform_runtime("browser")

    results: list[dict[str, Any]] = []
    for spec in case_specs:
        case = load_replay_case(suite, spec)
        print(f"case     : {spec['id']} — {spec['label']}")
        print(f"image    : {case.screenshot_path.relative_to(PROJECT_ROOT)}")
        for variant in variants:
            for sample in range(1, samples + 1):
                print(
                    f"[{variant} thinking={'on' if thinking else 'off'} "
                    f"tool_choice={tool_choice}] sample {sample}/{samples} ...",
                    flush=True,
                )
                result = run_variant(
                    case=case,
                    runtime=runtime,
                    variant=variant,
                    thinking=thinking,
                    tool_choice=tool_choice,
                    temperature=temperature,
                )
                result.update({
                    "case": spec["id"],
                    "sample": sample,
                    "expected_action_type": spec["expected_action"]["action_type"],
                    "source_run": case.source_run,
                })
                results.append(result)
                action_score = result.get("action_score") or {}
                status = "PASS" if result["ok"] else "FAIL"
                detail = result.get("error") or (
                    f"type={action_score.get('action_type_correct')} "
                    f"hit={action_score.get('target_hit')} "
                    f"distance={action_score.get('distance_to_recorded_point')}"
                )
                print(f"  {status} {detail}")

    by_variant = {
        variant: {
            "passed": sum(1 for item in results if item["variant"] == variant and item["ok"]),
            "total": sum(1 for item in results if item["variant"] == variant),
            "protocol_valid": sum(
                1
                for item in results
                if item["variant"] == variant and item["protocol_valid"]
            ),
            "with_tool": sum(
                1
                for item in results
                if item["variant"] == variant and not item["missing_tool"]
            ),
            "missing_tool": sum(
                1
                for item in results
                if item["variant"] == variant and item["missing_tool"]
            ),
            "with_content": sum(
                1
                for item in results
                if item["variant"] == variant and item["content_present"]
            ),
            "missing_state_content": sum(
                1
                for item in results
                if item["variant"] == variant and item["missing_state_content"]
            ),
            "target_hits": sum(
                1
                for item in results
                if item["variant"] == variant
                and (item.get("action_score") or {}).get("target_hit")
            ),
            "tool_name_correct": sum(
                1
                for item in results
                if item["variant"] == variant and item.get("tool_name_correct")
            ),
            "args_valid": sum(
                1
                for item in results
                if item["variant"] == variant and item.get("args_valid")
            ),
            "semantic_action_matches": sum(
                1
                for item in results
                if item["variant"] == variant
                and item.get("semantic_action_match")
            ),
        }
        for variant in variants
    }
    action_types = sorted({item["expected_action_type"] for item in results})
    by_action_type = {
        action_type: {
            variant: {
                "passed": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                    and item["ok"]
                ),
                "total": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                ),
                "protocol_valid": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                    and item["protocol_valid"]
                ),
                "with_tool": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                    and not item["missing_tool"]
                ),
                "missing_tool": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                    and item["missing_tool"]
                ),
                "with_content": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                    and item["content_present"]
                ),
                "semantic_action_matches": sum(
                    1
                    for item in results
                    if item["expected_action_type"] == action_type
                    and item["variant"] == variant
                    and item.get("semantic_action_match")
                ),
            }
            for variant in variants
        }
        for action_type in action_types
    }
    payload = {
        "experiment": suite["name"],
        "source_runs": sorted({item["source_run"] for item in results}),
        "thinking": thinking,
        "tool_choice": tool_choice,
        "temperature": temperature,
        "cases": [item["id"] for item in case_specs],
        "variants": variants,
        "results": results,
        "summary": by_variant,
        "summary_by_action_type": by_action_type,
    }
    if args.no_write:
        print(json.dumps(
            {"summary": by_variant, "summary_by_action_type": by_action_type},
            ensure_ascii=False,
            indent=2,
        ))
    else:
        output_dir = PROJECT_ROOT / suite["output_root"] / time.strftime("%Y%m%d_%H%M%S")
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "state_action_report.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report   : {output.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
