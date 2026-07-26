"""Runtime context builders for statement prompt assembly."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Callable, Iterable, MutableSequence  # Any: memory block duck-typing

from gui_agent.context.blocks import ContextBlock, ContextBudgeter

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


def statement_memory_block(memory: Any | None) -> ContextBlock | None:
    """Inject the Journal-projected StatementMemoryView into Transition."""
    if memory is None:
        return None
    render = getattr(memory, "render_prompt_section", None)
    if not callable(render):
        return None
    text = str(render() or "").strip()
    if not text:
        return None
    return ContextBlock(
        id="runtime.statement_memory",
        budget="required",
        source_type="runtime_state",
        source="statement_memory_view",
        ttl="turn",
        priority=22,
        metadata={
            "instance_id": str(getattr(memory, "instance_id", "") or ""),
            "statement_id": str(getattr(memory, "statement_id", "") or ""),
            "durable_count": len(getattr(memory, "durable_facts", ()) or ()),
        },
        content=text,
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
        source="statement.success",
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


def grid_status_block(tables: list[dict] | None) -> ContextBlock | None:
    """Inject DOM-derived grid record count into Transition context.

    Vision models may hallucinate "count unchanged / filter not applied" when the screenshot
    lacks a familiar filter-state indicator. Passing the DOM-authoritative total_records as
    structured text gives Transition an unambiguous textual signal — no screenshot reading
    required.
    """
    if not tables:
        return None
    lines: list[str] = []
    for t in tables:
        if not isinstance(t, dict):
            continue
        total = t.get("total_records")
        rows = t.get("row_count") or len(t.get("rows") or [])
        caption = str(t.get("caption") or t.get("source") or "网格").strip()
        if total is not None:
            partial = t.get("partial", False)
            lines.append(
                f"- {caption}: 当前页 {rows} 行，总记录数 {total}"
                + ("（部分，未完整采集）" if partial else "")
            )
        elif rows:
            lines.append(f"- {caption}: 当前页 {rows} 行（总记录数未知）")
    if not lines:
        return None
    content = "## 当前网格/表格记录数（DOM 权威值，非视觉推断）\n" + "\n".join(lines)
    return ContextBlock(
        id="runtime.observation.grid_status",
        budget="high",
        source_type="runtime_state",
        source="platform_adapter",
        ttl="turn",
        priority=29,
        content=content,
    )


def active_filters_block(form_controls: list[dict] | None) -> ContextBlock | None:
    """Inject populated filter inputs without claiming that they were submitted.

    Reads ``is_filter=True`` entries from form_controls (set by form_reader.js when the
    input is inside a grid filter area or its ID matches ``*_filter_*``).  Only entries
    with a non-empty value are included. Their DOM values are authoritative input facts,
    but unlike ``Observation.applied_filters`` they do not prove post-Apply state.

    Parallel to ``grid_status_block``: both replace screenshot-vision with DOM facts.
    """
    if not form_controls:
        return None
    lines: list[str] = []
    for item in form_controls:
        if not isinstance(item, dict):
            continue
        if not item.get("is_filter"):
            continue
        value = str(item.get("value") or item.get("selected_text") or "").strip()
        if not value:
            continue
        label = str(item.get("label") or item.get("name") or item.get("id") or "字段").strip()
        name = str(item.get("name") or "").strip()
        date_hint = " [日期 MM/DD/YYYY]" if item.get("is_datepicker") else ""
        name_part = f" (name={name})" if name and name != label else ""
        lines.append(f"- {label}{name_part}: {value!r}{date_hint}")
    if not lines:
        return None
    content = (
        "## 当前已填充的网格筛选输入（DOM 当前值，非视觉推断）\n"
        "以下 filter 输入框当前有非空值；这只证明输入框里已经有值，"
        "**不单独证明 Search/Apply 已提交或结果网格已刷新**。"
        "是否已经生效以“当前已生效筛选”或 Journal 中的提交回执为准。"
        "若值符合本步骤目标但尚无已生效证据，应提交当前筛选，而不是重复输入同一值。\n"
        + "\n".join(lines)
    )
    return ContextBlock(
        id="runtime.observation.active_filters",
        budget="high",
        source_type="runtime_state",
        source="platform_adapter",
        ttl="turn",
        priority=28,
        content=content,
    )


def applied_filter_state_block(
    applied_filters: dict[str, str] | None,
    applied_filter_meta: dict[str, Any] | None = None,
    initial_filters: dict[str, str] | None = None,
) -> ContextBlock | None:
    """Inject the currently-APPLIED filters as a deterministic fact.

    Distinct from ``active_filters_block`` (which reads filter INPUT-box values, framed as
    residuals to clear): this is the post-Apply state — the authoritative answer to "which
    filters are in EFFECT right now". Adapter-specific evidence may be a status indicator,
    encoded navigation state, filter-row state, or another platform-native mechanism. Whether the
    filtered field equals its target is what this state already encodes. Transition must judge a filter
    statement's progress from this state, NOT by re-reading a table display column (e.g. a display
    column derived from the filtered field, or a same-named neighbor computed on a different basis,
    which is a SEPARATE column)."""
    meta = applied_filter_meta or {}
    indicator_channel = str(meta.get("indicator_channel") or "").strip()
    fallback_channel = str(meta.get("fallback_channel") or "").strip()
    if not applied_filters:
        if indicator_channel != "absent" or fallback_channel != "present":
            return None
        content = (
            "## 已生效筛选证据通道\n"
            "当前页面缺少某种常见的筛选状态指示通道（这是适配器提供的 DOM 确定性事实），"
            "因此不能把“没看到该通道的证据”当作筛选未完成。\n"
            "判断筛选是否已生效时，请改用本页可用的确定性信号：筛选控件的 DOM 当前值、"
            "地址/状态编码、网格记录数/刷新结果。若这些信号显示目标筛选已经提交并产生非 0 结果，"
            "不要为了等待某个特定 UI 指示器而重复提交同一动作。"
        )
        return ContextBlock(
            id="runtime.observation.applied_filter_channel",
            budget="high",
            source_type="obs.dom",
            source="platform_adapter",
            ttl="turn",
            priority=27,
            authoritative_for=("filter.evidence_channel",),
            freshness="turn",
            coverage="complete",
            metadata={
                "state_indicator_channel": "absent",
                "state_fallback_channel": fallback_channel,
            },
            content=content,
        )
    # State-attribution annotation (deterministic run-level ledger): a chip present in the run's
    # FIRST applied-filters snapshot is the environment's INITIAL STATE — an observed fact with no
    # claim about where it came from; a chip that appeared DURING this run was established by this
    # task's own earlier steps — deliberate task scope. Without this fact the model may
    # free-guess which chips are "unrelated" and clear upstream scope every step (live run
    # 20260708_195215). No hygiene framing: the desired filter state is DEFINED by the current
    # statement; initial-state chips get reconciled to it, never "cleaned" for their own sake.
    provenance_note = ""
    if initial_filters is not None:
        def _prov(label: str, value: str) -> str:
            if initial_filters.get(label) == value:
                return "【任务开始时已生效——初始环境状态】"
            return "【本任务步骤设置——任务作用域】"
        lines = [f"- {label}: {value!r} {_prov(label, value)}" for label, value in applied_filters.items()]
        provenance_note = (
            "\n⚠️ 状态归属（确定性事实，不要自行推断来源）：『初始环境状态』只是任务开始时环境本来的样子，"
            "既不是垃圾也不代表意图——应该有什么筛选由**当前子目标**定义：筛选类子目标把已生效状态调整到"
            "它要求的终态（与终态冲突的初始条目在调整中被替换/清除，这是达成目标状态，不是打扫卫生）；"
            "非筛选类子目标（打开行/编辑/保存等）**不改动筛选状态**。"
            "『任务作用域』条目是本任务前序步骤特意建立的，任何子目标都不得当作无关条目清除。"
        )
    else:
        lines = [f"- {label}: {value!r}" for label, value in applied_filters.items()]
    source_line = "来源：平台 adapter 的已生效筛选状态。"
    content = (
        "## 当前已生效筛选（筛选控件权威状态）\n"
        "以下是网格筛选器**当前已应用**的条件（筛选已生效的确定性信号）：\n"
        + "\n".join(lines)
        + f"\n{source_line}"
        + "\n⚠️ 判断要点：'筛选是否已生效'由上面这些筛选控件状态决定，**不是**由表格里展示了哪些行/列决定。"
        "若本步骤要求的筛选已出现在上面，则筛选动作**已成功生效**；不要因为页面没有某种特定 UI 形态而否定它。"
        "行/单元格里某个**展示列**的值"
        "（如某个由被筛字段派生、或与之相邻同名却按不同口径计算的展示列，是另一列）**不得**用来推翻"
        "已生效的筛选、或据此要求重设/清除筛选——那只会打转。"
        + provenance_note
    )
    return ContextBlock(
        id="runtime.observation.applied_filter_state",
        budget="high",
        source_type="obs.dom",
        source="platform_adapter",
        ttl="turn",
        priority=28,
        # The adapter-normalized applied-filter state is authoritative for WHICH filters are
        # applied — NOT for which rows are currently rendered (a display column ≠ the filtered field).
        authoritative_for=("filter.applied",),
        not_authoritative_for=("table.rendered_rows",),
        freshness="turn",
        coverage="complete",
        metadata={},
        content=content,
    )


def form_controls_block(
    form_controls: list[dict] | None,
    metadata: dict | None = None,
) -> ContextBlock | None:
    text = format_form_controls_text(form_controls, metadata)
    if not text:
        return None
    return ContextBlock(
        id="runtime.observation.form_controls",
        budget="high",
        source_type="obs.dom",
        source="platform_adapter",
        ttl="turn",
        priority=30,
        # DOM is authoritative for a control's value/selection, NOT for whether it is visually
        # occluded / in the viewport (that is obs.vision). rendered_only: reports rendered controls,
        # not a guarantee of every control the page could render.
        authoritative_for=("control.value", "control.selected"),
        not_authoritative_for=("modal.visibility", "layout.visible_structure"),
        freshness="turn",
        coverage=str((metadata or {}).get("coverage") or "rendered_only"),
        metadata={"count": len(form_controls or []), **(metadata or {})},
        content=text,
    )


def format_form_controls_text(
    form_controls: list[dict] | None,
    metadata: dict | None = None,
) -> str:
    """Compact structured form-control inventory supplied by a platform adapter."""
    if not form_controls:
        return ""
    lines: list[str] = []
    for item in form_controls:
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
        placeholder = str(item.get("placeholder") or "").strip()
        bits = [f"{label}: {kind}"]
        name = str(item.get("name") or "").strip()
        if name and name != label:
            bits.append(f'name="{name}"')
        group_id = str(item.get("group_id") or "").strip()
        if group_id:
            group_field = str(item.get("group_field") or "").strip()
            group_index = item.get("group_index")
            group_bits = [f"row={group_id}"]
            if isinstance(group_index, int):
                group_bits.append(f"index={group_index}")
            if group_field:
                group_bits.append(f'field="{group_field}"')
            bits.append("group(" + ", ".join(group_bits) + ")")
        if kind == "native_select":
            # A native <select> (incl. <select multiple>): its SELECTION is DOM-authoritative and
            # is what Transition must judge on — say so explicitly so the model doesn't read the
            # still-visible option list as "not chosen yet". Empty = nothing selected.
            sel = str(item.get("selected_text") or "").strip()
            bits.append(f'已选中(DOM权威)="{sel}"' if sel else '已选中(DOM权威)=""(当前无选中项)')
        elif current:
            bits.append(f'current="{current}"')
        if item.get("is_datepicker") and placeholder:
            bits.append(f'placeholder="{placeholder}" [日期 MM/DD/YYYY]')
        elif item.get("is_datepicker"):
            bits.append("[日期 MM/DD/YYYY]")
        if item.get("focused") is True:
            bits.append("focused=true")
        if item.get("required") is True:
            bits.append("required=true(DOM权威)")
        if item.get("in_viewport") is False:
            vp = item.get("viewport_pos")
            if vp == "above":
                bits.append("[需向上滚动到视口]")
            elif vp == "below":
                bits.append("[需向下滚动到视口]")
            else:
                bits.append("[需先滚动到视口]")
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
    inventory_claim = (
        "这些控件是当前页已渲染控件的**部分采样**；清单缺少某控件不能证明页面不存在该控件。"
        if (metadata or {}).get("coverage") == "partial" or (metadata is None and len(form_controls) >= 40)
        else "这些控件涵盖当前页适配器本轮返回的已渲染可编辑控件。"
    )
    return (
        "## 浏览器 DOM 表单控件（适配器感知，不是截图文本）\n"
        + inventory_claim
        + "给出类型、当前值和候选项。"
        "标 `[需向上滚动到视口]` / `[需向下滚动到视口]` / `[需先滚动到视口]` 的控件**确实存在于表单中**，只是不在当前视口——"
        "要操作它先按标注的方向滚动到它（`[需向上滚动到视口]`=控件在视口上方，向上滚；`[需向下滚动到视口]`=在下方，向下滚），"
        "**不要因为它不在当前截图里就判它「不存在」/「缺失」，也不要盲目往一个方向滚**。\n"
        "⚠️ `*_input` 文本框的 `current=` 是它**实际内容的权威，优先级高于截图像素**："
        "窄文本框在截图里可能只显示滚动后的尾部（已输入完整目标词时，框窄可能只显示尾部片段）——"
        "**只要某 `*_input` 文本框的 `current=` 等于目标值，就是已正确输入完整内容，判该输入已达成；不要据截图把它判成「输入不完整/缺前缀/被截断/需重输」**。\n"
        "⚠️ 带 `group(row=..., field=...)` 的控件属于重复表单集合。集合成员必须按**同一 row 内的具体 field/name**判断；"
        "目标文本出现在同一行的另一个字段，不能证明目标字段已经填写。\n"
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
