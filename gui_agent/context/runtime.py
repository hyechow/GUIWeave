"""Runtime context builders for milestone prompt assembly."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Iterable, MutableSequence

from gui_agent.context.blocks import ContextBlock, ContextBudgeter
from gui_agent.core.schemas import Milestone, PolicyTurn

# Hard char ceiling for the dynamic context blocks assembled around a prompt. Generous by
# default (insurance against runaway inflation — knowledge blobs + history + @file refs piling
# up), env-overridable for tuning. char≈token upper bound for CJK, so this keeps the block
# portion well inside the model window. Lower it once real-run peaks are observed.
DEFAULT_CONTEXT_BLOCKS_MAX_CHARS = int(os.environ.get("CONTEXT_BLOCKS_MAX_CHARS") or 80_000)


def current_date_block(now: datetime | None = None) -> ContextBlock:
    now = now or datetime.now()
    return ContextBlock(
        id="runtime.current_date",
        budget="low",
        source_type="runtime_state",
        source="clock",
        ttl="turn",
        priority=10,
        content=f"当前日期：{now.strftime('%Y年%m月%d日 %A')}",
    )


def history_block(
    history: list[PolicyTurn],
    *,
    limit: int = 8,
    current_milestone_id: str | None = None,
    recent_n: int = 6,
) -> ContextBlock:
    return ContextBlock(
        id="runtime.history.recent_actions",
        budget="medium",
        source_type="runtime_state",
        source="policy_history",
        ttl="session",
        priority=40,
        metadata={"limit": limit, "milestone_id": current_milestone_id or ""},
        content=(
            "## 历史操作记录\n"
            + format_history_text(
                history, limit=limit, current_milestone_id=current_milestone_id, recent_n=recent_n
            )
        ),
    )


def milestone_block(
    milestone: Milestone,
    *,
    task_type: str | None = None,
    scroll_stop_condition: str | None = None,
    retry_count: int | None = None,
) -> ContextBlock:
    """Current milestone/task state shared by checker/planner/replanner/loop prompts."""
    lines = [
        "## 当前子目标",
        f"- 名称：{milestone.name}",
        f"- 描述：{milestone.description}",
    ]
    if milestone.success_condition:
        lines.append(f"- 验收条件：{milestone.success_condition}")
    if scroll_stop_condition:
        lines.append(f"- 停止条件：{scroll_stop_condition}")
    if milestone.kind:
        lines.append(f"- 子目标类型：{milestone.kind}")
    if milestone.completion_strategy:
        lines.append(f"- 完成策略：{milestone.completion_strategy}")
    if task_type:
        lines.append(f"- 任务类型：{task_type}")
    if retry_count is not None:
        lines.append(f"- 已重试次数：{retry_count}")
    return ContextBlock(
        id="runtime.milestone.current",
        budget="required",
        source_type="runtime_state",
        source="milestone",
        ttl="turn",
        priority=20,
        metadata={"milestone_id": milestone.id, "kind": milestone.kind},
        content="\n".join(lines),
    )


def constraints_block(constraints: Iterable[str] | None) -> ContextBlock | None:
    items = [str(item).strip() for item in constraints or [] if str(item).strip()]
    if not items:
        return None
    return ContextBlock(
        id="runtime.constraints.global",
        budget="required",
        source_type="runtime_state",
        source="supervisor_constraints",
        ttl="task",
        priority=30,
        metadata={"count": len(items)},
        content="## 全局约束\n" + "\n".join(f"- {item}" for item in items),
    )


def app_identity_block(app_name: str) -> ContextBlock | None:
    if not app_name:
        return None
    return ContextBlock(
        id="runtime.app.identity_hint",
        budget="high",
        source_type="runtime_state",
        source="app_binding",
        ttl="task",
        priority=25,
        content=(
            "## 应用身份辅助\n"
            f"任务目标涉及「{app_name}」应用；页面/界面识别仍必须以当前可见内容为准，"
            "不要预设当前就在目标应用内。"
        ),
    )


def checker_kind_rules_block(kind_section: str) -> ContextBlock | None:
    if not kind_section:
        return None
    return ContextBlock(
        id="prompt.milestone.check_kind_rules",
        budget="required",
        source_type="prompt_context",
        source="milestone.check_kind_sections",
        ttl="turn",
        priority=35,
        content=kind_section,
    )


def checker_result_block(check: Any) -> ContextBlock:
    issues = getattr(check, "issues", None) or []
    missing = getattr(check, "missing_evidence", None) or []
    visible = getattr(check, "visible_evidence", None) or []
    lines = [
        "## 当前验收结果",
        f"- status：{getattr(check, 'status', '')}",
        f"- reason：{getattr(check, 'reason', '')}",
        f"- issues：{json_text(issues)}",
        f"- missing_evidence：{json_text(missing)}",
        f"- visible_evidence：{json_text(visible)}",
        f"- page_identity：{getattr(check, 'page_identity', '')}",
        f"- 当前屏幕摘要：{getattr(check, 'summary', '')}",
    ]
    return ContextBlock(
        id="runtime.checker.result",
        budget="required",
        source_type="runtime_state",
        source="checker",
        ttl="turn",
        priority=25,
        content="\n".join(lines),
    )


def replan_state_block(
    check: Any,
    *,
    retry_count: int,
    failure_hints: Iterable[str] | None = None,
) -> ContextBlock:
    issues = getattr(check, "issues", None) or []
    lines = [
        "## 重规划状态",
        f"- 未达成原因：{getattr(check, 'stuck_reason', '') or getattr(check, 'reason', '')}",
        f"- 具体问题：{json_text(issues)}",
        f"- 已重试次数：{retry_count}",
        f"- 可能未达成原因提示：{json_text(list(failure_hints or []))}",
    ]
    return ContextBlock(
        id="runtime.replan.state",
        budget="required",
        source_type="runtime_state",
        source="replanner",
        ttl="turn",
        priority=25,
        content="\n".join(lines),
    )


def format_history_text(
    history: list[PolicyTurn],
    *,
    limit: int = 8,
    current_milestone_id: str | None = None,
    recent_n: int = 6,
) -> str:
    """Render policy history for a prompt.

    Default (current_milestone_id=None): the legacy flat last-``limit`` window — kept so
    reports / tests / no-milestone callers are unchanged.

    Relevant-history (current_milestone_id set): the current milestone's last ``recent_n`` turns
    in full detail, preceded by ONE compressed state line per earlier milestone (its last known
    summary = its done-summary). This keeps the immediately useful detail while collapsing old
    action-by-action history that bloats long runs. Failure/dead-end signal is handled separately
    by the planner's tried-instructions + replan-diagnosis injection, so it is not duplicated here."""
    if not history:
        return "（无历史记录，这是第一轮）"
    if current_milestone_id is None:
        return _render_turn_lines(history[-limit:])

    current = [t for t in history if t.supervisor.milestone_id == current_milestone_id]
    prior = [t for t in history if t.supervisor.milestone_id != current_milestone_id]
    detail_turns = (current or history)[-recent_n:]

    parts: list[str] = []
    prior_summary = _completed_milestones_text(prior)
    if prior_summary:
        parts.append("已完成/早前子目标进展（每子目标压成一行）：\n" + prior_summary)
        parts.append("当前子目标最近操作：\n" + _render_turn_lines(detail_turns))
    else:
        parts.append(_render_turn_lines(detail_turns))
    return "\n".join(parts)


def _completed_milestones_text(turns: list[PolicyTurn]) -> str:
    """One compressed line per earlier milestone (in first-seen order): its last known summary
    (collection_summary preferred). Collapses old turn-by-turn history into milestone state."""
    last_by_mid: dict[str, PolicyTurn] = {}
    order: list[str] = []
    for t in turns:
        mid = t.supervisor.milestone_id or "?"
        if mid not in last_by_mid:
            order.append(mid)
        last_by_mid[mid] = t
    lines = []
    for mid in order:
        sv = last_by_mid[mid].supervisor
        summary = (sv.collection_summary or sv.summary or "").strip()
        lines.append(f"- [{mid}] {summary}".rstrip())
    return "\n".join(lines)


def _render_turn_lines(turns: list[PolicyTurn]) -> str:
    lines = []
    for idx, turn in enumerate(turns):
        sv = turn.supervisor
        next_sv = turns[idx + 1].supervisor if idx + 1 < len(turns) else None
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


def extra_instruction_block(extra: str, *, source: str = "guard") -> ContextBlock | None:
    if not extra:
        return None
    return ContextBlock(
        id="runtime.output_correction",
        budget="required",
        source_type="runtime_state",
        source=source,
        ttl="turn",
        priority=20,
        content=f"## 输出修正要求\n{extra}",
    )


def page_title_block(title: str | None) -> ContextBlock | None:
    if not title:
        return None
    return ContextBlock(
        id="runtime.observation.page_title",
        budget="high",
        source_type="runtime_state",
        source="observation.title",
        ttl="turn",
        priority=30,
        content=(
            "## 附加页面标题（不在截图里，仅作页面身份辅助信号；仍需结合可见内容判断）\n"
            f"- 当前页面标题：{title}"
        ),
    )


def acceptance_items_block(items: list[str]) -> ContextBlock | None:
    if not items:
        return None
    enumerated = "\n".join(f"{i}) {text}" for i, text in enumerate(items, 1))
    return ContextBlock(
        id="runtime.acceptance.checklist",
        budget="required",
        source_type="runtime_state",
        source="milestone.success_condition",
        ttl="turn",
        priority=35,
        metadata={"count": len(items)},
        content=(
            "## 逐项验收（填入 item_verdicts）\n"
            "对下列每个验收子项独立判定：met（是否满足）+ 一句可见证据，按对应 index 填入 item_verdicts。"
            "逐项判定不改变你对整体 status 的综合判断。\n"
            f"{enumerated}"
        ),
    )


def knowledge_block(kind: str, content: str | None, *, source: str = "knowledge_base") -> ContextBlock | None:
    if not content:
        return None
    titles = {
        "app_navigation": "## 应用导航知识",
        "page_elements": "## 页面元素知识",
        "check_rules": (
            "## 应用验收观察规则（来自知识库，描述该应用界面的实际显示形态与完成标志；"
            "这是最终解释规则；若它与通用规则或逐项验收的字面理解冲突，以本节对界面事实的解释为准）"
        ),
    }
    return ContextBlock(
        id=f"knowledge.{kind}",
        budget="high" if kind == "check_rules" else "medium",
        source_type="knowledge_base",
        source=source,
        ttl="session",
        priority=50,
        content=f"{titles.get(kind, f'## {kind}')}\n{content}",
    )


def form_controls_block(form_controls: list[dict] | None) -> ContextBlock | None:
    text = format_form_controls_text(form_controls)
    if not text:
        return None
    return ContextBlock(
        id="runtime.observation.form_controls",
        budget="high",
        source_type="runtime_state",
        source="platform_adapter",
        ttl="turn",
        priority=30,
        metadata={"count": len(form_controls or [])},
        content=text,
    )


def format_form_controls_text(form_controls: list[dict] | None) -> str:
    """Compact structured form-control inventory supplied by a platform adapter."""
    if not form_controls:
        return ""
    lines: list[str] = []
    for item in form_controls[:25]:
        if not isinstance(item, dict):
            continue
        label = str(
            item.get("label")
            or item.get("name")
            or item.get("id")
            or item.get("placeholder")
            or "未命名控件"
        ).strip()
        kind = str(item.get("kind") or "control").strip()
        current = str(item.get("selected_text") or item.get("value") or "").strip()
        bits = [f"{label}: {kind}"]
        if current or kind == "native_select":
            bits.append(f'current="{current}"')
        if item.get("focused") is True:
            bits.append("focused=true")
        options = item.get("options")
        if isinstance(options, list) and options:
            shown = [str(opt) for opt in options[:20]]
            suffix = ", ..." if len(options) > len(shown) else ""
            bits.append("options=[" + ", ".join(shown) + suffix + "]")
        rect = item.get("rect")
        if isinstance(rect, dict) and isinstance(rect.get("x"), int) and isinstance(rect.get("y"), int):
            bits.append(f"center=({rect['x']},{rect['y']})")
        lines.append("- " + "; ".join(bits))
    if not lines:
        return ""
    return (
        "## 浏览器 DOM 表单控件（适配器感知，不是截图文本）\n"
        "这些控件由当前平台适配器提供，只包含可见可编辑控件的类型、当前值和候选项。"
        "若某控件可由适配器直接设置候选值，应规划为“选择/设置 <字段> 为 <选项>”，"
        "不要规划为“点击展开后等待选项可见”。\n"
        "⚠️ `*_input` 文本框的 `current=` 是它**实际内容的权威，优先级高于截图像素**："
        "窄文本框在截图里可能只显示滚动后的尾部（已输入完整目标词时，框窄可能只显示尾部片段）——"
        "**只要某 `*_input` 文本框的 `current=` 等于目标值，就是已正确输入完整内容，判该输入已达成；不要据截图把它判成「输入不完整/缺前缀/被截断/需重输」**。\n"
        + "\n".join(lines)
    )


def task_goal_block(goal: str) -> ContextBlock:
    return ContextBlock(
        id="runtime.task.goal",
        budget="required",
        source_type="runtime_state",
        source="user_goal",
        ttl="task",
        priority=20,
        content=f"用户任务：{goal}",
    )


def file_reference_block(file_section: str) -> ContextBlock | None:
    if not file_section:
        return None
    return ContextBlock(
        id="runtime.task.file_refs",
        budget="required",
        source_type="file_reference",
        source="goal_at_refs",
        ttl="task",
        priority=25,
        content=file_section,
    )


def browser_page_block(url: str | None, title: str | None, *, site: str = "") -> ContextBlock | None:
    if not url and not title and not site:
        return None
    page = "## 当前前台页面（以此为准，截图看不到地址栏）"
    if site:
        page += f"\n站点：{site}（已知应用）"
    if url:
        page += f"\nurl：{url}"
    if title:
        page += f"\n页面：{title}"
    return ContextBlock(
        id="runtime.observation.browser_page",
        budget="high",
        source_type="runtime_state",
        source="observation",
        ttl="turn",
        priority=30,
        content=page,
    )


def feedback_block(issues: Iterable[str]) -> ContextBlock | None:
    issue_list = [issue for issue in issues if issue]
    if not issue_list:
        return None
    body = "\n".join(f"  - {issue}" for issue in issue_list)
    return ContextBlock(
        id="runtime.decompose.feedback",
        budget="required",
        source_type="runtime_state",
        source="decomposition_guard",
        ttl="turn",
        priority=20,
        metadata={"count": len(issue_list)},
        content=f"上一轮分解存在以下问题，请修正：\n{body}",
    )


def completed_milestones_block(milestones: Iterable[Milestone], *, current_id: str = "") -> ContextBlock:
    lines = [
        f"  - [{m.id}] {m.name}（已完成，不要退回到该状态）"
        for m in milestones
        if m.status == "done" and m.id != current_id
    ]
    return ContextBlock(
        id="runtime.milestones.completed",
        budget="medium",
        source_type="runtime_state",
        source="milestone_state",
        ttl="session",
        priority=40,
        content="## 已完成的子目标（不要退回这些状态）\n" + ("\n".join(lines) if lines else "  （无）"),
    )


def tried_instructions_block(instructions: Iterable[str]) -> ContextBlock:
    items = sorted({item for item in instructions if item})
    content = "\n".join(f"  - 「{item}」" for item in items) if items else "  （无）"
    return ContextBlock(
        id="runtime.history.tried_instructions",
        budget="medium",
        source_type="runtime_state",
        source="policy_history",
        ttl="session",
        priority=40,
        metadata={"count": len(items)},
        content="## 已尝试但尚未达成的指令\n" + content,
    )


def loop_frame_summary_block(summary: str) -> ContextBlock:
    return ContextBlock(
        id="runtime.loop.frame_summary",
        budget="required",
        source_type="runtime_state",
        source="loop_checker",
        ttl="turn",
        priority=30,
        content="## 当前屏幕状态\n" + (summary or "（无当前屏幕摘要）"),
    )


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_prompt_context(
    blocks: Iterable[ContextBlock | None],
    *,
    max_chars: int | None = None,
    label: str = "context",
    say: Callable[[str], None] = print,
    report_sink: MutableSequence[dict] | Callable[[dict], None] | None = None,
) -> str:
    """Render context blocks under a hard char ceiling (the ContextBudgeter, drop-only).

    All call sites go through here, so there is no bare/unbudgeted render path. Dropped blocks
    are logged so the trace shows exactly what was shed. The default ceiling is generous; pass
    ``max_chars`` to tighten per call site."""
    budgeter = ContextBudgeter(max_chars or DEFAULT_CONTEXT_BLOCKS_MAX_CHARS)
    result = budgeter.apply(blocks)
    if report_sink is not None and result.decisions:
        _append_report(report_sink, result.to_report(label=label))
    if result.dropped:
        names = "、".join(f"{b.id}[{b.budget}]({len(b.render())}字)" for b in result.dropped)
        say(f"  [ContextBudget] {label} 超预算({budgeter.max_chars}字),丢弃 {len(result.dropped)} 块: {names}")
    if result.over_budget:
        say(
            f"  [ContextBudget] ⚠️ {label} 必留(required)块已达 {result.kept_chars} 字 / 上限 "
            f"{budgeter.max_chars} 字,无可丢弃块"
        )
    return result.text


def _append_report(
    sink: MutableSequence[dict] | Callable[[dict], None],
    report: dict,
) -> None:
    if callable(sink):
        sink(report)
    else:
        sink.append(report)
