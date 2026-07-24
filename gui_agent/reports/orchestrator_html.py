"""HTML helpers for orchestrator and non-UI report sections."""

from __future__ import annotations

import json

from gui_agent.core.config import pricing_currency

from .html_utils import _attr, _safe
from .metrics import _fmt_tokens, _sum_tokens, _token_cost
from .prompt_html import _render_module_io_html

_PROG_KIND_BADGE = {
    "interact": "statement-badge-action",
    "acquire": "statement-badge-collection",
    "read": "statement-badge-collection",
    "source_check": "statement-badge-collection",
    "command": "statement-badge-navigation",
}

def _program_run_items(stmts: list) -> list[dict]:
    """Flatten run statements from a DSL program, preserving source order."""
    out: list[dict] = []
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        op = s.get("op", "")
        if op in {"interact", "acquire", "read", "source_check", "command"}:
            out.append(s)
        elif op == "if":
            out.extend(_program_run_items(s.get("then", [])))
            out.extend(_program_run_items(s.get("otherwise", [])))
        elif op == "foreach":
            out.extend(_program_run_items(s.get("body", [])))
    return out


def _field_summary(fields: list[str]) -> str:
    shown = ", ".join(fields[:12])
    suffix = f", … +{len(fields) - 12}" if len(fields) > 12 else ""
    return f"{len(fields)} fields [{shown}{suffix}]"


def _report_value(value: object, *, sample_items: int, limit: int) -> str:
    """Bound data-plane values in HTML while keeping a useful report projection."""
    items = value
    prefix = ""
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        items = value["rows"]
        prefix = "rows: "
    if isinstance(items, list) and items and all(isinstance(item, dict) for item in items):
        fields = list(dict.fromkeys(str(key) for row in items for key in row))
        summary = f"{prefix}{len(items)} records · {_field_summary(fields)}"
        if sample_items:
            sample = json.dumps(
                items[:sample_items], ensure_ascii=False, indent=2, default=str,
            )
            summary += f"\nsample (first {min(len(items), sample_items)}):\n{sample}"
        return summary
    if isinstance(items, list) and len(items) > 8:
        summary = f"{prefix}{len(items)} items"
        if sample_items:
            sample = json.dumps(items[:sample_items], ensure_ascii=False, indent=2, default=str)
            summary += f"\nsample (first {sample_items}):\n{sample}"
        return summary
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, indent=2 if sample_items else None, default=str,
    )
    if len(text) <= limit:
        return text
    if isinstance(value, dict):
        return f"object · {_field_summary(list(map(str, value)))}"
    return text[:limit - 3] + "…"


def _render_non_ui_detail(non_ui: dict) -> str:
    kind = str(non_ui.get("executor") or "non_ui")
    mode = {
        "acquire": "Acquire 集合采集",
        "read": "Read 观察绑定",
        "source_check": "SourceCheck 字段检查",
        "command": "确定性命令",
    }.get(kind, "非交互")
    outputs = non_ui.get("outputs") if isinstance(non_ui.get("outputs"), dict) else {}
    fields = list(outputs)
    evidence = [str(value) for value in (non_ui.get("evidence") or [])]
    read_rows = []
    for field in fields:
        read_rows.append(
            f'<div class="nonui-read"><span class="nonui-key">{_safe(field)}</span>'
            f'<pre class="nonui-val">{_safe(_report_value(outputs.get(field, ""), sample_items=2, limit=2400))}</pre></div>'
        )
    reads_html = f'<div class="nonui-reads">{"".join(read_rows)}</div>' if read_rows else ""
    evidence_html = (
        f'<pre class="nonui-code">{_safe(chr(10).join(evidence))}</pre>'
        if evidence else ""
    )
    return (
        f'<div class="nonui-detail">'
        f'<div class="nonui-title">{_safe(mode)}</div>'
        f'{reads_html}'
        f'{evidence_html}'
        f'</div>'
    )


def _render_orchestrator_context_reports(orchestrator: dict) -> str:
    reports = orchestrator.get("context_reports") or []
    if not reports:
        return '<div class="compat-row"><span class="compat-chip">未记录 prompt snapshot</span></div>'
    token_usage = orchestrator.get("token_usage") if isinstance(orchestrator.get("token_usage"), dict) else {}
    if not token_usage:
        token_usage = _estimate_orchestrator_token_usage(reports)
    return _render_module_io_html(reports, token_usage)


def _render_orchestrator_metrics(orchestrator: dict) -> str:
    token_usage = orchestrator.get("token_usage") if isinstance(orchestrator.get("token_usage"), dict) else {}
    estimated = False
    if not token_usage:
        token_usage = _estimate_orchestrator_token_usage(orchestrator.get("context_reports") or [])
        estimated = bool(token_usage)
    ti, to = _sum_tokens(token_usage)
    timings = orchestrator.get("timings") if isinstance(orchestrator.get("timings"), dict) else {}
    total_s = sum(float(v or 0) for v in timings.values())
    calls = int(orchestrator.get("llm_calls") or 0) or _count_prompt_calls(orchestrator.get("context_reports") or [])

    parts: list[str] = []
    if total_s > 0:
        parts.append(f"{total_s:.1f}s")
    elif calls or ti or to:
        parts.append('<span class="compat-chip">耗时未记录</span>')
    if calls:
        parts.append(f"{calls} call{'s' if calls != 1 else ''}")
    if ti or to:
        prefix = "≈" if estimated else ""
        cost_prefix = "≈"
        parts.append(f"{prefix}{_fmt_tokens(ti)}/{_fmt_tokens(to)} tok")
        parts.append(f"{cost_prefix}{pricing_currency()}{_token_cost(token_usage):.4f}")
    if not parts:
        return ""
    return f'<span class="statement-time">{" · ".join(parts)}</span>'


def _estimate_orchestrator_token_usage(reports: list[dict]) -> dict:
    input_chars = 0
    output_chars = 0
    for report in reports:
        if not isinstance(report, dict):
            continue
        if report.get("kind") == "prompt_snapshot":
            for role in report.get("roles") or []:
                if not isinstance(role, dict):
                    continue
                for part in role.get("parts") or []:
                    if isinstance(part, dict) and part.get("type") != "image":
                        input_chars += int(part.get("chars") or len(str(part.get("text") or "")))
        elif report.get("kind") == "llm_output":
            output_chars += int(report.get("chars") or len(str(report.get("raw_output") or "")))
    input_tokens = _estimate_tokens(input_chars)
    output_tokens = _estimate_tokens(output_chars)
    if not input_tokens and not output_tokens:
        return {}
    return {"orchestrator.decompose": {"input": input_tokens, "output": output_tokens}}


def _count_prompt_calls(reports: list[dict]) -> int:
    return sum(1 for report in reports if isinstance(report, dict) and report.get("kind") == "prompt_snapshot")


def _estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return max(1, (chars + 3) // 4)


def _render_program_card(
    orchestrator: dict,
    program: dict,
    h2: str,
    name: str,
    *,
    directive: str = "",
    metrics_html: str = "",
    extras_html: str = "",
    h2_style: str = "",
    embedded: bool = False,
) -> str:
    """Render one DSL program using the final report-only Program result projection."""

    # var -> captured values. This projection is written once at Program finish; it is never a
    # live runtime/checkpoint authority.
    env: dict[str, dict] = {}
    for r in (orchestrator.get("report_run_log") or []):
        result = r.get("result") or {}
        outputs = result.get("outputs") or {}
        if r.get("var") and outputs:
            env[r["var"]] = outputs

    counter = [0]

    def _result_html(s: dict) -> str:
        var = s.get("bind")
        returned = list((s.get("returns") or {}).keys())
        if not returned:
            return ""
        values = env.get(var or "") or {}
        shown = "、".join(
            (
                f"{_safe(field)}={_safe(_report_value(values[field], sample_items=0, limit=240))}"
                if values.get(field) is not None else _safe(field)
            )
            for field in returned
        )
        return f'<span class="prog-ret">→ {_safe("result" if len(returned) == 1 else "outputs")} {shown}</span>'

    def _run_row(s: dict) -> str:
        counter[0] += 1
        kind = s.get("op", "interact")
        badge = _PROG_KIND_BADGE.get(kind, "statement-badge-default")
        name = s.get("goal") or s.get("capability", "")
        if not name and kind == "read":
            name = "、".join(
                str(binding.get("name") or output)
                for output, binding in (s.get("reads") or {}).items()
            )
        if not name and kind == "source_check":
            name = "、".join(str(field) for field in s.get("required_fields") or [])
        return (
            f'<div class="prog-step">'
            f'<span class="prog-n">{counter[0]}</span>'
            f'<span class="prog-name">{_safe(name)}</span>'
            f'<span class="statement-badge {badge}">{_safe(kind)}</span>{_result_html(s)}'
            f'</div>'
        )

    def _source_macro(items: list, index: int) -> tuple[dict, dict, dict, dict] | None:
        nodes = items[index:index + 4]
        if len(nodes) != 4 or not all(isinstance(node, dict) for node in nodes):
            return None
        initial, branch, final, acquire = nodes
        if not (
            [node.get("op") for node in nodes]
            == ["source_check", "if", "source_check", "acquire"]
            and str(initial.get("bind") or "").startswith("__source_")
            and str(final.get("bind") or "").startswith("__source_")
            and (acquire.get("source_check") or {}).get("var") == final.get("bind")
        ):
            return None
        return initial, branch, final, acquire

    def _availability(s: dict) -> tuple[object, str]:
        values = env.get(str(s.get("bind") or "")) or {}
        available = values.get("available")
        return available, {True: "可读", False: "不可读"}.get(available, "未运行")

    def _acquire_macro_row(
        macro: tuple[dict, dict, dict, dict], consumer: dict | None,
    ) -> str:
        initial, _, final, acquire = macro
        counter[0] += 1
        fields = list(acquire.get("required_fields") or initial.get("required_fields") or [])
        field_html = (
            f'<span class="compat-chip">字段 · {_safe("、".join(map(str, fields)))}</span>'
            if fields else ""
        )
        returns = acquire.get("returns") or {}
        coverage = next(
            (spec.get("coverage") for spec in returns.values() if isinstance(spec, dict)),
            "complete",
        )
        coverage_label = "完整集合" if coverage == "complete" else "尽力采集"
        goal = (consumer or {}).get("goal") or acquire.get("goal") or "当前业务集合"
        initial_value, initial_status = _availability(initial)
        _, final_status = _availability(final)
        repair_status = {True: "已跳过", False: "已执行"}.get(initial_value, "按需执行")
        return (
            f'<div class="nonui-detail">'
            f'<div class="prog-step">'
            f'<span class="prog-n">{counter[0]}</span>'
            f'<span class="prog-name">采集{_safe(coverage_label)}：{_safe(goal)}</span>'
            f'<span class="statement-badge statement-badge-collection">acquire</span>'
            f'{_result_html(acquire)}'
            f'</div>'
            f'<div class="compat-row">{field_html}</div>'
            f'<details class="prog-compiler-detail">'
            f'<summary>Compiler 自动步骤 · 来源检查 + 必要时修复</summary>'
            f'<div><b>初检</b> · {_safe(initial_status)}</div>'
            f'<div><b>修复</b> · {_safe(repair_status)}</div>'
            f'<div><b>复检</b> · {_safe(final_status)}</div>'
            f'</details>'
            f'</div>'
        )

    def _walk(items: list) -> list[str]:
        out: list[str] = []
        index = 0
        while index < len(items):
            macro = _source_macro(items, index)
            if macro is not None:
                consumer = items[index + 4] if index + 4 < len(items) else None
                out.append(_acquire_macro_row(
                    macro,
                    consumer if isinstance(consumer, dict) and consumer.get("op") == "read" else None,
                ))
                index += 4
                continue
            s = items[index]
            op = s.get("op", "")
            if op in {"interact", "acquire", "read", "source_check", "command"}:
                out.append(_run_row(s))
            elif op == "if":
                cond = s.get("cond", {})
                ref = cond.get("ref") or {}
                path = json.dumps(ref.get("path") or [], ensure_ascii=False)
                c = (f'<span class="prog-condvar">{_safe(ref.get("var",""))}{_safe(path)}</span>'
                     f' {_safe(cond.get("cmp","=="))} '
                     f'<span class="prog-condval">{_safe(cond.get("value",""))}</span>')
                then_html = "".join(_walk(s.get("then", []))) or '<div class="prog-step prog-empty">—</div>'
                else_html = "".join(_walk(s.get("otherwise", [])))
                # empty else (no otherwise statements) → omit the whole else block, don't show "else : —"
                else_block = (
                    f'<div class="prog-cond"><span class="prog-kw">else :</span></div>'
                    f'<div class="prog-branch prog-branch-else">{else_html}</div>'
                ) if else_html else ""
                out.append(
                    f'<div class="prog-if">'
                    f'<div class="prog-cond"><span class="prog-kw">if</span> {c} <span class="prog-kw">:</span></div>'
                    f'<div class="prog-branch">{then_html}</div>'
                    f'{else_block}'
                    f'</div>'
                )
            elif op == "foreach":
                items = s.get("items") or {}
                over = f"{items.get('var', '')}{items.get('path', [])}"
                into = s.get("into") or ""
                values = env.get(into, []) if into else []
                n = len(values) if isinstance(values, list) else 0
                collected = f'<span class="prog-ret">→ 采集 {n} 行</span>' if n else ""
                body_html = "".join(_walk(s.get("body", []))) or '<div class="prog-step prog-empty">—</div>'
                head = (
                    f'<span class="prog-kw">foreach</span> '
                    f'<span class="prog-condvar">{_safe(s.get("item","item"))}</span> '
                    f'<span class="prog-kw">in</span> '
                    f'<span class="prog-condvar">{_safe(over)}</span> '
                    f'<span class="prog-kw">→</span> '
                    f'<span class="prog-condval">{_safe(into)}</span>{collected}'
                )
                out.append(
                    f'<div class="prog-if">'
                    f'<div class="prog-cond">{head} <span class="prog-kw">:</span></div>'
                    f'<div class="prog-branch">{body_html}</div>'
                    f'</div>'
                )
            elif op == "finish":
                message = str(s.get("message") or "").strip()
                output_names = list((s.get("outputs") or {}).keys())
                conclusion = (
                    message
                    or (f'完成并返回 {"、".join(map(str, output_names))}' if output_names else "完成任务")
                )
                out.append(f'<div class="prog-finish">↩ {_safe(conclusion)}</div>')
            index += 1
        return out

    counter[0] = 0
    card_body = "".join(_walk(program.get("statements") or []))
    g = _safe(program.get("goal") or "")
    input_html = (
        f'<div class="prog-input"><span class="prog-input-label">输入</span>{g}'
        f'<span class="prog-input-arrow">↓ 执行计划</span></div>'
    ) if g else ""
    # A re-decompose card leads with WHY it fired (the Feasibility kick-back directive).
    directive_html = (
        '<div class="prog-input" style="border-left:3px solid #e0a020;background:#fff8e8">'
        '<span class="prog-input-label">重编排触发</span>'
        f'<span style="color:#a05a00">⚠️ 上层判 statement 不可行 → 踢回指令：{_safe(directive)}</span>'
        '</div>'
    ) if directive else ""
    if embedded:
        # Integrated INTO a statement's 验收结果 — a light sub-block (the re-decompose outcome),
        # NOT an independent #vN statement card. The new plan itself also runs as the subsequent
        # executed statements; this block makes the trigger→new-plan explicit in the 验收 area.
        return (
            '<div style="margin-top:4px">'
            '<div style="display:flex;align-items:center;gap:8px;padding:2px 0 6px 0">'
            f'<span style="color:#dc2626;font-weight:800;font-size:14px">↻ {_safe(h2)}</span>'
            f'<span style="color:#991b1b;font-weight:700;font-size:13px">{_safe(name)}</span>'
            f'<span style="margin-left:auto">{metrics_html}</span></div>'
            f'<div class="prog-body">{directive_html}{input_html}{card_body}{extras_html}</div>'
            '</div>'
        )
    anchor = h2.replace("#", "").replace("↻", "r")
    h2_open = f'<h2 style="{h2_style}">' if h2_style else "<h2>"
    return (
        f'<div class="statement prog-section" id="ms-orchestrate-{anchor}">'
        f'<div class="statement-header">'
        f'{h2_open}{_safe(h2)}</h2>'
        f'<span class="statement-name">{_safe(name)}</span>'
        f'<span class="statement-badge statement-badge-default">program</span>'
        f'{metrics_html}'
        f'</div>'
        f'<div class="prog-body">{directive_html}{input_html}{card_body}{extras_html}</div>'
        f'</div>'
    )


def _render_program_section(orchestrator: dict | None) -> str:
    """Render the original DSL or coding plan before its executed statements."""
    if not orchestrator:
        return ""
    prog0 = orchestrator.get("program") or {}
    if prog0.get("kind") == "coding" or prog0.get("source"):
        return _render_coding_program_card(orchestrator, prog0)
    if not prog0.get("statements"):
        return ""
    return _render_program_card(
        orchestrator, prog0, "#0", "任务编排 · 业务步骤与控制流",
        metrics_html=_render_orchestrator_metrics(orchestrator),
        extras_html=_render_orchestrator_context_reports(orchestrator),
    )


def _render_coding_program_card(orchestrator: dict, program: dict) -> str:
    source = str(program.get("source") or "").strip()
    if not source:
        return ""
    goal = str(program.get("goal") or "")
    input_html = (
        f'<div class="prog-input"><span class="prog-input-label">输入</span>{_safe(goal)}'
        '<span class="prog-input-arrow">↓ Python 计划</span></div>'
        if goal else ""
    )
    review = next(
        (
            report for report in reversed(orchestrator.get("context_reports") or [])
            if isinstance(report, dict) and report.get("kind") == "coding_review"
        ),
        {},
    )
    if review.get("repaired"):
        review_label = "Review · 已修复"
    elif review.get("approved"):
        review_label = "Review · 通过"
    elif review:
        review_label = "Review · 未通过"
    else:
        review_label = "Review · 未记录"
    review_html = (
        '<div class="compat-row">'
        f'<span class="compat-chip">{_safe(review_label)}</span>'
        '<span class="coding-note">运行时产生的 Statement 调用与执行证据见下方时间线</span>'
        '</div>'
    )
    return (
        '<div class="statement prog-section" id="ms-orchestrate-coding">'
        '<div class="statement-header">'
        '<h2>#0</h2>'
        '<span class="statement-name">Coding Orchestrator · Python 执行计划</span>'
        '<span class="statement-badge statement-badge-default">python</span>'
        f'{_render_orchestrator_metrics(orchestrator)}'
        '</div>'
        f'<div class="prog-body">{input_html}{review_html}'
        f'<pre class="coding-source"><code>{_safe(source)}</code></pre>'
        f'{_render_orchestrator_context_reports(orchestrator)}'
        '</div>'
        '</div>'
    )


def render_redecompose_card(orchestrator: dict | None, kickback_n) -> str:
    """A timeline DIVIDER marking where a Feasibility kick-back re-decomposed the plan. The new
    plan's steps are NOT listed here — they ARE the statements that FOLLOW this marker; the banner
    just marks the transition (trigger + directive + model-call cost) so the subsequent statements
    read as the v{N} plan, without duplicating them."""
    if not orchestrator:
        return ""
    rd = next(
        (r for r in (orchestrator.get("redecomposes") or []) if r.get("kickback_n") == kickback_n),
        None,
    )
    if not rd or not (rd.get("program") or {}).get("statements"):
        return ""
    at = rd.get("at_turn")
    # Render the replacement plan with the same semantic-node style as the #0 card (reuse
    # _render_program_card), in a light embedded block — always visible, not folded.
    return _render_program_card(
        orchestrator, rd["program"], f"#v{kickback_n}",
        f"重编排 · Re-decompose（T{at} 后 · Feasibility 踢回）",
        directive=rd.get("directive") or "",
        metrics_html=_render_orchestrator_metrics(rd),
        extras_html=_render_orchestrator_context_reports(rd),  # 模型调用详情 (this re-decompose's LLM trace)
        embedded=True,
    )
