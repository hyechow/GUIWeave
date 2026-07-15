"""HTML helpers for orchestrator and non-UI report sections."""

from __future__ import annotations

import json
import re

from gui_agent.core.config import pricing_currency

from .html_utils import _attr, _safe
from .metrics import _fmt_tokens, _sum_tokens, _token_cost
from .prompt_html import _render_module_io_html

_PROG_KIND_BADGE = {
    "navigation": "statement-badge-navigation", "filter": "statement-badge-filter",
    "action": "statement-badge-action", "read": "statement-badge-collection",
    "data_query": "statement-badge-collection",
}

# {var[field]} data-flow template (mirrors the runner's program.TEMPLATE_RE): the report uses it
# to show what each read captured and to resolve action targets the way the runner did at runtime.
_PROG_TEMPLATE_RE = re.compile(r"\{(\w+)\[([^\]]+)\]\}")


def _program_run_items(stmts: list) -> list[dict]:
    """Flatten run statements from a DSL program, preserving source order."""
    out: list[dict] = []
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        op = s.get("op", "run")
        if op == "run":
            out.append(s)
        elif op == "if":
            out.extend(_program_run_items(s.get("then", [])))
            out.extend(_program_run_items(s.get("otherwise", [])))
        elif op == "foreach":
            out.extend(_program_run_items(s.get("body", [])))
    return out


def _program_run_meta(record: dict, run_items: list[dict]) -> dict:
    var = str(record.get("var") or "")
    name = str(record.get("name") or "")
    if var:
        for item in run_items:
            if str(item.get("var") or "") == var:
                return item
    if name:
        for item in run_items:
            if str(item.get("name") or "") == name:
                return item
    return {}


def _pretty_non_ui_value(value: object) -> str:
    text = str(value if value is not None else "")
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _render_non_ui_detail(non_ui: dict) -> str:
    kind = str(non_ui.get("kind") or "non_ui")
    mode = "非交互"
    sql = str(non_ui.get("sql") or "")
    scope = str(non_ui.get("data_scope") or "")
    read_spec = str(non_ui.get("read_spec") or "")
    reads = non_ui.get("reads") if isinstance(non_ui.get("reads"), dict) else {}
    returns = [str(v) for v in (non_ui.get("returns") or []) if str(v)]
    fields = returns or list(reads.keys())
    sql_html = ""
    if sql:
        scope_html = f' <span style="color:#94a3b8">scope={_safe(scope)}</span>' if scope else ""
        sql_html = (
            f'<div class="nonui-sql"><span class="nonui-label">SQL{scope_html}</span>'
            f'<pre class="nonui-code">{_safe(sql)}</pre></div>'
        )
    spec_html = (
        f'<div class="nonui-sql"><span class="nonui-label">SPEC</span>'
        f'<pre class="nonui-code">{_safe(read_spec)}</pre></div>'
        if read_spec else ""
    )
    read_rows = []
    for field in fields:
        read_rows.append(
            f'<div class="nonui-read"><span class="nonui-key">{_safe(field)}</span>'
            f'<pre class="nonui-val">{_safe(_pretty_non_ui_value(reads.get(field, "")))}</pre></div>'
        )
    reads_html = f'<div class="nonui-reads">{"".join(read_rows)}</div>' if read_rows else ""
    return (
        f'<div class="nonui-detail">'
        f'<div class="nonui-title">{mode} · {_safe(kind)}</div>'
        f'{sql_html}{spec_html}{reads_html}'
        f'</div>'
    )


def _render_non_ui_log(orchestrator: dict, run_items: list[dict]) -> str:
    rows: list[str] = []
    for idx, record in enumerate(orchestrator.get("run_log") or [], start=1):
        if not isinstance(record, dict):
            continue
        meta = _program_run_meta(record, run_items)
        kind = str(meta.get("kind") or "")
        if kind not in {"read", "data_query"}:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        completed = result.get("phase") == "completed"
        status_cls = "nonui-ok" if completed else "nonui-fail"
        status_text = "completed" if completed else "failed"
        badge = _PROG_KIND_BADGE.get(kind, "statement-badge-default")
        name = str(record.get("name") or meta.get("name") or "")
        var = str(record.get("var") or meta.get("var") or "")
        var_html = f'<span class="prog-var">{_safe(var)} =</span> ' if var else ""
        summary = str((result or {}).get("summary") or "")
        summary_html = f'<div class="nonui-summary">{_safe(summary)}</div>' if summary else ""
        sql = str(meta.get("sql") or "")
        scope = str(meta.get("data_scope") or "")
        sql_html = ""
        if kind == "data_query" and sql:
            scope_html = f' <span style="color:#94a3b8">scope={_safe(scope)}</span>' if scope else ""
            sql_html = (
                f'<div class="nonui-sql"><span class="nonui-label">SQL{scope_html}</span>'
                f'<pre class="nonui-code">{_safe(sql)}</pre></div>'
            )
        reads = result.get("reads") if isinstance(result.get("reads"), dict) else {}
        read_rows = []
        for field in (meta.get("returns") or list(reads.keys()) or []):
            val = reads.get(field, "")
            read_rows.append(
                f'<div class="nonui-read"><span class="nonui-key">{_safe(str(field))}</span>'
                f'<pre class="nonui-val">{_safe(_pretty_non_ui_value(val))}</pre></div>'
            )
        reads_html = f'<div class="nonui-reads">{"".join(read_rows)}</div>' if read_rows else ""
        obs = str(record.get("observation_url") or "")
        shot_html = (
            f'<div class="nonui-shot"><img src="{_safe(obs)}" onclick="zoomImg(\'{_safe(obs)}\')" '
            f'alt="非 UI 读取帧"></div>'
            if obs else ""
        )
        rows.append(
            f'<div class="nonui-row">'
            f'<div class="nonui-main">'
            f'<div class="nonui-head">'
            f'<span class="nonui-n">#{idx}</span>'
            f'<span class="nonui-name">{var_html}{_safe(name)}</span>'
            f'<span class="statement-badge {badge}">{_safe(kind)}</span>'
            f'<span class="nonui-status {status_cls}">{status_text}</span>'
            f'</div>'
            f'{summary_html}{sql_html}{reads_html}'
            f'</div>{shot_html}</div>'
        )
    if not rows:
        return ""
    return (
        f'<div class="nonui-log">'
        f'<div class="nonui-title">非 UI 执行记录</div>'
        f'{"".join(rows)}'
        f'</div>'
    )


def _render_orchestrator_context_reports(orchestrator: dict) -> str:
    reports = orchestrator.get("context_reports") or []
    if not reports:
        return '<div class="compat-row"><span class="compat-chip">旧日志缺 prompt snapshot</span></div>'
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
    """Render ONE DSL program as a statement-style card (#0 or #0↻N). env (var→reads, from the
    runner's mirrored run_log) is shared across a run's cards so {var[field]} refs resolve as the
    runner did at execute time."""

    # var -> {field: value} captured by each completed read (runner mirrors interp.run_log into
    # context.orchestrator). Lets the report show WHAT a read got and resolve {var[field]} action
    # targets — a pure read has no turn/statement, so without this the report only had the static
    # program structure, never the values it read or where they flowed.
    env: dict[str, dict] = {}
    rows_by_var: dict[str, list] = {}   # foreach `into` / list_read → accumulated rows (for "采集 N 行")
    for r in (orchestrator.get("run_log") or []):
        result = r.get("result") or {}
        reads = result.get("reads") or {}
        if r.get("var") and reads:
            env[r["var"]] = reads
        rows = result.get("rows") or []
        if r.get("var") and rows:
            rows_by_var[r["var"]] = rows

    def _resolve(text: str) -> str:
        """Substitute {var[field]} from env (as the runner did at execute time); keep the raw
        ref when unresolved so the data-flow wiring stays visible."""
        def _sub(m: "re.Match[str]") -> str:
            vals = env.get(m.group(1))
            return vals.get(m.group(2).strip().strip("'\""), m.group(0)) if vals else m.group(0)
        return _PROG_TEMPLATE_RE.sub(_sub, text or "")

    counter = [0]

    def _run_row(s: dict) -> str:
        counter[0] += 1
        kind = s.get("kind", "action")
        badge = _PROG_KIND_BADGE.get(kind, "statement-badge-default")
        var = s.get("var")
        var_html = f'<span class="prog-var">{_safe(var)} =</span> ' if var else ""
        name = s.get("name", "")
        # show the authored name (template intact = the program), then the runtime-resolved target
        # next to it when {var[field]} filled (so 编辑 {r[实际名称]} → lucas-10003 is visible)
        resolved = _resolve(name)
        resolved_html = (
            f'<span class="prog-resolved">▸ {_safe(resolved)}</span>' if resolved != name else ""
        )
        ret = [r for r in (s.get("returns") or []) if r]
        list_html = ""
        if kind == "read" and s.get("list_read"):
            n = len(rows_by_var.get(var or "", []))
            count = f" {n} 行" if n else ""
            list_html = f'<span class="statement-badge statement-badge-default" style="background:#eef">列表读取{count}</span>'
        if kind in {"read", "data_query"} and ret:
            vals = env.get(var or "") or {}
            verb = "查" if kind == "data_query" else "读"
            shown = "、".join(
                f"{_safe(f)}={_safe(vals[f])}" if vals.get(f) else _safe(f) for f in ret
            )
            ret_html = f'<span class="prog-ret">→ {verb} {shown}</span>'
        else:
            ret_html = ""
        return (
            f'<div class="prog-step">'
            f'<span class="prog-n">{counter[0]}</span>'
            f'<span class="prog-name">{var_html}{_safe(name)}</span>{resolved_html}'
            f'<span class="statement-badge {badge}">{_safe(kind)}</span>{list_html}{ret_html}'
            f'</div>'
        )

    def _walk(items: list) -> list[str]:
        out = []
        for s in items:
            op = s.get("op", "run")
            if op == "run":
                out.append(_run_row(s))
            elif op == "if":
                cond = s.get("cond", {})
                c = (f'<span class="prog-condvar">{_safe(cond.get("var",""))}[{_safe(cond.get("field",""))}]</span>'
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
                over = s.get("over", "")
                into = s.get("into") or (f"{s.get('var','')}s")
                n = len(rows_by_var.get(into, []))
                collected = f'<span class="prog-ret">→ 采集 {n} 行</span>' if n else ""
                body_html = "".join(_walk(s.get("body", []))) or '<div class="prog-step prog-empty">—</div>'
                head = (
                    f'<span class="prog-kw">foreach</span> '
                    f'<span class="prog-condvar">{_safe(s.get("var",""))}</span> '
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
                out.append(f'<div class="prog-finish">↩ finish「{_safe(s.get("message", ""))}」</div>')
        return out

    counter[0] = 0
    card_body = "".join(_walk(program.get("statements") or []))
    g = _safe(program.get("goal") or "")
    input_html = (
        f'<div class="prog-input"><span class="prog-input-label">输入</span>{g}'
        f'<span class="prog-input-arrow">↓ 分解为</span></div>'
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
        f'<div class="prog-body">{directive_html}{input_html}{extras_html}{card_body}</div>'
        f'</div>'
    )


def _render_program_section(orchestrator: dict | None) -> str:
    """Render the ORIGINAL decomposed program as the #0 编排 card (before the executed statements).
    Re-decompose cards are NOT here — each renders INLINE right after the statement that triggered it
    (render_redecompose_card), so it sits where it actually fired. Empty → no section."""
    if not orchestrator:
        return ""
    prog0 = orchestrator.get("program") or {}
    if not prog0.get("statements"):
        return ""
    return _render_program_card(
        orchestrator, prog0, "#0", "编排 · decompose → DSL program",
        metrics_html=_render_orchestrator_metrics(orchestrator),
        extras_html=_render_orchestrator_context_reports(orchestrator),
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
    # Render the new plan with the SAME run/if/finish step style as the #0 card (reuse
    # _render_program_card), in a light embedded block — always visible, not folded.
    return _render_program_card(
        orchestrator, rd["program"], f"#v{kickback_n}",
        f"重编排 · Re-decompose（T{at} 后 · Feasibility 踢回）",
        directive=rd.get("directive") or "",
        metrics_html=_render_orchestrator_metrics(rd),
        extras_html=_render_orchestrator_context_reports(rd),  # 模型调用详情 (this re-decompose's LLM trace)
        embedded=True,
    )
