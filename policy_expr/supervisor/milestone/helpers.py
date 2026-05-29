import base64
import json
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.schemas import Milestone, Observation, PolicyTurn

from .schemas import _PlanResult, _SingleCheckResult
from .prompts import (
    CHECK_KIND_SECTIONS,
    PLAN_PROMPT,
    SINGLE_CHECKER_PROMPT,
    _CHECK_SECTION_DEFAULT,
)

load_dotenv()


def _format_history(history: list[PolicyTurn]) -> str:
    if not history:
        return "（无历史记录，这是第一轮）"
    recent = history[-8:]
    lines = []
    for idx, turn in enumerate(recent):
        sv = turn.supervisor
        next_sv = recent[idx + 1].supervisor if idx + 1 < len(recent) else None
        result = next_sv.summary if next_sv else "（结果尚未记录）"
        failed = (
            turn.executed
            and next_sv
            and next_sv.milestone_id == sv.milestone_id
            and ("卡住" in (next_sv.summary or "") or "重试" in (next_sv.summary or ""))
        )
        prefix = "❌ " if failed else ""
        if turn.action_decision and turn.executed:
            action = turn.action_decision.action
            outcome = f"导致错误: {result}" if failed else f"结果: {result}"
            lines.append(
                f"{turn.index}. {prefix}指令=「{sv.instruction}」"
                f" → [{action.action_type}] {action.description}"
                f" → {outcome}"
            )
        elif turn.action_decision and not turn.executed:
            action = turn.action_decision.action
            lines.append(
                f"{turn.index}. {prefix}指令=「{sv.instruction}」 → [未执行] [{action.action_type}] {action.description}"
            )
        else:
            lines.append(f"{turn.index}. [跳过动作] {sv.summary} → 结果: {result}")
    return "\n".join(lines)


def _make_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


def _build_msgs(system_prompt: str, png_bytes: bytes) -> list:
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    b64 = base64.b64encode(resize_to_logical_png(png_bytes)).decode()
    return [
        SystemMessage(content=f"{system_prompt}\n\n当前日期：{today}"),
        HumanMessage(content=[
            {"type": "text", "text": "请根据当前屏幕做出决策。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]


def _inject_knowledge(
    msgs: list,
    app_knowledge: str | None,
    elements_knowledge: str | None,
) -> None:
    """Inject navigation and elements knowledge into the user message."""
    parts: list[dict] = []
    if app_knowledge:
        parts.append({"type": "text", "text": f"## 应用导航知识\n{app_knowledge}\n\n"})
    if elements_knowledge:
        parts.append({"type": "text", "text": f"## 页面元素知识\n{elements_knowledge}\n\n"})
    if parts:
        msgs[1].content = parts + msgs[1].content


def run_checker(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    app_name: str = "",
    task_type: str = "action",
    constraints: Optional[list[str]] = None,
    extra: str = "",
) -> _SingleCheckResult:
    """Run the single-step milestone checker. Used by both production and evals."""
    if constraints is None:
        constraints = []
    app_name_context = f"任务目标涉及「{app_name}」应用，" if app_name else ""
    kind_section = CHECK_KIND_SECTIONS.get(milestone.kind, _CHECK_SECTION_DEFAULT)
    prompt = SINGLE_CHECKER_PROMPT.format(
        milestone_name=milestone.name,
        milestone_desc=milestone.description,
        success_condition=milestone.success_condition,
        milestone_kind=milestone.kind,
        completion_strategy=milestone.completion_strategy,
        task_type=task_type,
        constraints=json.dumps(constraints, ensure_ascii=False),
        history_text=_format_history(history),
        app_name_context=app_name_context,
        kind_section=kind_section,
    )
    if extra:
        prompt += f"\n\n## 输出修正要求\n{extra}"
    result = invoke_structured(_make_llm(), _build_msgs(prompt, observation.png_bytes), _SingleCheckResult)

    if result.status == "done" and (not result.visible_evidence or result.missing_evidence):
        print("  [SingleCheck] done 缺少证据，重试...")
        result = run_checker(
            milestone, observation, history,
            app_name=app_name, task_type=task_type, constraints=constraints,
            extra="你刚才返回 done 但 visible_evidence 为空或 missing_evidence 非空。请重新核对截图，确有证据才能 done，否则返回 in_progress 或 stuck。",
        )
    if result.status == "done" and (not result.visible_evidence or result.missing_evidence):
        return _SingleCheckResult(
            status="stuck",
            reason="checker 返回 done 但缺少可见验收证据",
            stuck_reason="done 缺少可见证据",
            summary=result.summary,
        )
    return result


def run_planner(
    milestone: Milestone,
    check: _SingleCheckResult,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    constraints: Optional[list[str]] = None,
    extra: str = "",
    app_knowledge: Optional[str] = None,
    elements_knowledge: Optional[str] = None,
) -> _PlanResult:
    """Run the step planner. Used by both production and evals."""
    if constraints is None:
        constraints = []
    if milestone.retry_count > 0 and not extra:
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone.id
        })
        # Collect dead-end paths from ALL milestones (replan diagnoses)
        dead_ends: list[str] = []
        for t in history:
            if t.replan and t.replan.get("diagnosis"):
                dead_ends.append(t.replan["diagnosis"])
        if tried:
            tried_lines = "\n".join(f"  - 「{i}」" for i in tried)
            extra = (
                f"⚠️ 该子目标已重试 {milestone.retry_count} 次。以下操作在本子目标中已全部尝试过"
                f"（含导致失败或死路的路径），请务必选择完全不同的路径：\n{tried_lines}"
            )
        if dead_ends:
            dedup = list(dict.fromkeys(dead_ends))
            dead_end_lines = "\n".join(f"  - {d}" for d in dedup)
            extra_text = (
                "⚠️ 以下路径已被确认为死路，禁止再次尝试：\n"
                f"{dead_end_lines}"
            )
            extra = f"{extra}\n\n{extra_text}" if extra else extra_text
    prompt = PLAN_PROMPT.format(
        milestone_name=milestone.name,
        milestone_desc=milestone.description,
        success_condition=milestone.success_condition,
        milestone_kind=milestone.kind,
        constraints=json.dumps(constraints, ensure_ascii=False),
        check_status=check.status,
        check_reason=check.reason,
        issues=json.dumps(check.issues, ensure_ascii=False),
        missing_evidence=json.dumps(check.missing_evidence, ensure_ascii=False),
        check_summary=check.summary,
        history_text=_format_history(history),
    )
    if extra:
        prompt += f"\n\n## 输出修正要求\n{extra}"
    msgs = _build_msgs(prompt, observation.png_bytes)
    _inject_knowledge(msgs, app_knowledge, elements_knowledge)
    return invoke_structured(_make_llm(), msgs, _PlanResult)
