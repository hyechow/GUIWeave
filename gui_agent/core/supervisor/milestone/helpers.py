import base64
import json
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.policies.base import resize_to_logical_png
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn

from .schemas import MilestonePrompts, _LoopFrameResult, _PlanResult, _SingleCheckResult


def _default_milestone_prompts() -> MilestonePrompts:
    """Lazy iphone-prompts default: keeps every no-prompts caller (iphone factory,
    evals, tests, scripts) working unchanged while the prompt STRINGS live in the
    iphone adapter — not core. A platform that wants its own prompts injects them."""
    from gui_agent.adapters.iphone.supervisor.milestone.prompts import (
        IPHONE_MILESTONE_PROMPTS,
    )
    return IPHONE_MILESTONE_PROMPTS


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
        unmet = (
            turn.executed
            and next_sv
            and next_sv.milestone_id == sv.milestone_id
            and (
                "卡住" in (next_sv.summary or "")
                or "重试" in (next_sv.summary or "")
                or "尚未达成" in (next_sv.summary or "")
                or "调整策略" in (next_sv.summary or "")
            )
        )
        prefix = "⚠️ " if unmet else ""
        if turn.action_decision and turn.executed:
            action = turn.action_decision.action
            outcome = f"未达成: {result}" if unmet else f"结果: {result}"
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


def _prepare_prompt_png(png_bytes: bytes, image_resize: str = "retina") -> bytes:
    if image_resize == "none":
        return png_bytes
    return resize_to_logical_png(png_bytes)


def _build_msgs(system_prompt: str, png_bytes: bytes, *, image_resize: str = "retina") -> list:
    today = datetime.now().strftime("%Y年%m月%d日 %A")
    b64 = base64.b64encode(_prepare_prompt_png(png_bytes, image_resize)).decode()
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


def _normalize_picker_plan_direction(plan: _PlanResult) -> _PlanResult:
    """Make structured picker direction consistent with current/target values.

    The LLM sometimes chooses the right picker column and values but flips the
    value direction wording. The executor relies on direction for scroll polarity,
    so normalize known numeric picker columns here; policy.py also recomputes the
    step count later.
    """
    column = (getattr(plan, "drag_column", None) or "").strip().lower()
    cur = getattr(plan, "drag_current_value", None)
    tgt = getattr(plan, "drag_target_value", None)
    if not column or cur is None or tgt is None or cur == tgt:
        return plan
    if column == "minute":
        forward = (tgt - cur) % 60
        backward = (cur - tgt) % 60
        plan.direction = "increase" if forward <= backward else "decrease"
    elif column == "hour":
        forward = (tgt - cur) % 12
        backward = (cur - tgt) % 12
        plan.direction = "increase" if forward <= backward else "decrease"
    else:
        plan.direction = "increase" if tgt > cur else "decrease"
    return plan


def run_checker(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    app_name: str = "",
    task_type: str = "action",
    constraints: Optional[list[str]] = None,
    extra: str = "",
    _is_retry: bool = False,
    prompts: Optional[MilestonePrompts] = None,
    section_manifest: str = "",
) -> _SingleCheckResult:
    """Run the single-step milestone checker. Used by both production and evals.

    ``section_manifest`` (progressive knowledge): when given, the section list is appended so
    the checker also picks ``relevant_sections`` for the same turn's planner to load on demand.
    """
    if prompts is None:
        prompts = _default_milestone_prompts()
    if constraints is None:
        constraints = []
    app_name_context = f"任务目标涉及「{app_name}」应用，" if app_name else ""
    kind_section = prompts.check_kind_sections.get(milestone.kind, prompts.check_section_default)
    # 连续调值类（picker 收敛）在 kind 段之上叠加专用段：当前值以滚轮中心带为准、强制输出
    # 当前值/目标值。这是连续操作进展传感器的基础——避免把已推进的拖动误读为"没动"。
    if milestone.is_converge:
        kind_section = kind_section + prompts.check_section_converge
    prompt = prompts.single_checker.format(
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
    if section_manifest:
        prompt += f"\n\n{section_manifest}"
    # Inject the tab TITLE (the viewport-language page name the screenshot doesn't show) as
    # an auxiliary identity signal, so the checker does not need to infer it from pixels. The URL is deliberately NOT
    # injected — a machine URL adds little discriminating value as LLM text and costs tokens; it
    # is consumed programmatically instead (url-change = navigation, in the supervisor). Only
    # browser perception supplies a title; iphone/android leave it None and nothing is injected.
    title = getattr(observation, "title", None)
    if title:
        prompt += (
            "\n\n## 附加页面标题（不在截图里，仅作页面身份辅助信号；仍需结合可见内容判断）\n"
            f"- 当前页面标题：{title}"
        )
    result = invoke_structured(
        _make_llm(),
        _build_msgs(prompt, observation.png_bytes, image_resize=prompts.image_resize),
        _SingleCheckResult,
    )

    def _strip_progress_evidence(r: _SingleCheckResult) -> None:
        # 连续调值类(is_converge)的 checker section 要求把「当前值=/目标值=」写进 missing_evidence
        # 作进展传感器。这些在 done(值已达标)时是冗余的、不是真正缺失的验收证据；若留着会被下面
        # 的 done 守卫当成「证据不足」而每次 done 都误触发一次重试(实测频繁,~1s/次)。done 时剔除它们。
        if milestone.is_converge and r.status == "done" and r.missing_evidence:
            r.missing_evidence = [
                e for e in r.missing_evidence if "当前值" not in e and "目标值" not in e
            ]

    _strip_progress_evidence(result)

    # Validate a done verdict in two stages, because the retry and the force-stuck
    # play different roles:
    #
    # _retry_worthy — triggers exactly ONE re-verification. For non-navigation kinds
    # an empty visible_evidence is included here: a *wrong* done on a pre-action
    # screen (e.g. send button visible but not yet sent) typically can't cite real
    # evidence, and forcing a re-check makes the model recant to in_progress
    # (measured: send-screen wrong-done 4/10 → 0/10). This is the actual
    # hallucination catcher — the recant on re-verify, not the force-stuck.
    #
    # _still_invalid — after the retry, only HARD contradictions force stuck:
    # missing_evidence non-empty (self-contradiction) or a too-thin reason. We do
    # NOT force stuck on empty visible_evidence here: the prompt declares that field
    # optional, so a legitimate done that survives re-verification (date really IS
    # set, page identity really IS right) but cited its evidence in reason/summary
    # rather than the optional array must be accepted — else we kill a correct done
    # and lock the subgoal (observed: 20260530_094941 turn7 → cascaded task failure).
    def _retry_worthy(r: _SingleCheckResult) -> bool:
        if r.missing_evidence or len((r.reason or "").strip()) < 10:
            return True
        # 连续调值类(converge)的 done 由「滚轮中间行值 == success_condition 目标」直接验证，
        # 证据是客观可读的(写在 reason 里)，不需要 visible_evidence 数组——豁免该条，否则每个
        # converge done 都会因 visible_evidence 空而白白重试一次。
        if milestone.is_converge:
            return False
        return milestone.kind != "navigation" and not r.visible_evidence

    def _still_invalid(r: _SingleCheckResult) -> bool:
        return bool(r.missing_evidence) or len((r.reason or "").strip()) < 10

    if not _is_retry and result.status == "done" and _retry_worthy(result):
        # Retry exactly once. The retry passes _is_retry=True so it skips this
        # block — without that the recursion would re-trigger and retry unboundedly
        # (observed up to 4×). Capped at 2 LLM calls total.
        print("  [SingleCheck] done 证据不足，重试...")
        result = run_checker(
            milestone, observation, history,
            app_name=app_name, task_type=task_type, constraints=constraints,
            extra=(
                "你刚才判定为 done，请重新核对截图确认验收条件是否*已经发生*（而非仅具备执行条件）。"
                "若确实满足，请在 reason 里写清你看到的具体依据（标题文字、高亮选中项、已设定的值、"
                "结果提示），并清空 missing_evidence；若截图只显示「可以执行」但结果尚未出现，改判 in_progress。"
            ),
            _is_retry=True,
            prompts=prompts,
        )
        _strip_progress_evidence(result)
    if result.status == "done" and _still_invalid(result):
        return _SingleCheckResult(
            status="stuck",
            reason="当前验收结论缺少可见依据或存在自相矛盾",
            stuck_reason="当前页面仍缺少足够的验收依据，需要继续确认可见状态",
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
    prompts: Optional[MilestonePrompts] = None,
) -> _PlanResult:
    """Run the step planner. Used by both production and evals."""
    if prompts is None:
        prompts = _default_milestone_prompts()
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
                f"⚠️ 该子目标已尝试 {milestone.retry_count} 次。以下操作在本子目标中已经尝试过但尚未达成验收条件，"
                f"请优先选择当前截图中不同的可见入口或下一步元素：\n{tried_lines}"
            )
        if dead_ends:
            dedup = list(dict.fromkeys(dead_ends))
            dead_end_lines = "\n".join(f"  - {d}" for d in dedup)
            extra_text = (
                "⚠️ 以下路径之前未达成目标，除非当前截图出现新的明确证据，否则不要重复：\n"
                f"{dead_end_lines}"
            )
            extra = f"{extra}\n\n{extra_text}" if extra else extra_text
    prompt = prompts.plan.format(
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
    msgs = _build_msgs(prompt, observation.png_bytes, image_resize=prompts.image_resize)
    _inject_knowledge(msgs, app_knowledge, elements_knowledge)
    plan_schema = prompts.plan_result_schema or _PlanResult
    return _normalize_picker_plan_direction(invoke_structured(_make_llm(), msgs, plan_schema))


def run_loop_check(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    constraints: Optional[list[str]] = None,
    prompts: Optional[MilestonePrompts] = None,
) -> _LoopFrameResult:
    """Run the per-frame scroll_until_boundary assessment. Used by both production and evals."""
    if prompts is None:
        prompts = _default_milestone_prompts()
    prompt = prompts.loop_frame.format(
        milestone_name=milestone.name,
        milestone_desc=milestone.description,
        scroll_stop_condition=milestone.scroll_stop_condition or "滚动至列表物理底部时停止",
        constraints=json.dumps(constraints or [], ensure_ascii=False),
        history_text=_format_history(history),
    )
    return invoke_structured(
        _make_llm(),
        _build_msgs(prompt, observation.png_bytes, image_resize=prompts.image_resize),
        _LoopFrameResult,
    )
