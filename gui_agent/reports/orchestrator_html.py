"""HTML helpers for orchestrator and non-UI report sections."""

from __future__ import annotations

import ast
import json
import re

from gui_agent.core.config import pricing_currency

from .html_utils import _safe
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


def is_coding_orchestrator(orchestrator: dict | None) -> bool:
    if not orchestrator:
        return False
    prog = orchestrator.get("program") or {}
    return prog.get("kind") == "coding" or bool(str(prog.get("source") or "").strip())


def _coding_statement_id(record: dict) -> str:
    """Resolve statement id from a report_run_log entry (new + legacy logs)."""
    node_id = str(record.get("node_id") or "").strip()
    if node_id:
        return node_id
    instance_id = str(record.get("instance_id") or "").strip()
    if ":" in instance_id:
        return instance_id.rsplit(":", 1)[-1]
    return instance_id


def _render_program_section(orchestrator: dict | None) -> str:
    """Render the original DSL or coding plan before its executed statements."""
    if not orchestrator:
        return ""
    prog0 = orchestrator.get("program") or {}
    if is_coding_orchestrator(orchestrator):
        return _render_coding_program_shell(orchestrator, prog0)
    if not prog0.get("statements"):
        return ""
    return _render_program_card(
        orchestrator, prog0, "#0", "任务编排 · 业务步骤与控制流",
        metrics_html=_render_orchestrator_metrics(orchestrator),
        extras_html=_render_orchestrator_context_reports(orchestrator),
    )


def _infer_coding_op(
    *,
    coding_op: str = "",
    executor: str = "",
    inputs: dict | None = None,
    name: str = "",
) -> str:
    """Resolve ctx.* op for a statement (new logs: coding_op; old logs: infer)."""
    if coding_op:
        return str(coding_op)
    inputs = inputs if isinstance(inputs, dict) else {}
    if "lookup_request" in inputs:
        return "lookup"
    if "constrain_request" in inputs:
        return "constrain"
    if executor == "acquire":
        return "acquire"
    if executor == "read":
        return "read"
    if executor == "command":
        return "command"
    if executor == "interact":
        if isinstance(inputs.get("values"), dict) and inputs.get("values"):
            return "write"
        return "gui"
    # Legacy report_run_log often omits executor/coding_op — recover from goal text.
    lowered = name.lower()
    if "resolve collection" in lowered or "locate collection" in lowered:
        return "lookup"
    if "materialize records" in lowered:
        return "acquire"
    if "narrow collection" in lowered or "filter" in lowered and "collection" in lowered:
        return "constrain"
    if name and executor:
        return executor
    if name and len(name) <= 48 and "\n" not in name:
        # Short imperative labels like go_to / open_settings map to ctx.gui.
        return "gui"
    return executor or ""


def _flatten_coding_inputs(
    *,
    coding_op: str,
    coding_payload: dict | None,
    inputs: dict | None,
) -> dict:
    """Prefer coding_payload; fall back to statement.inputs / nested request objects."""
    if isinstance(coding_payload, dict) and coding_payload:
        return dict(coding_payload)
    inputs = inputs if isinstance(inputs, dict) else {}
    if not inputs:
        return {}
    for key in ("lookup_request", "constrain_request"):
        nested = inputs.get(key)
        if isinstance(nested, dict) and nested:
            return dict(nested)
    return dict(inputs)


def _coding_call_label(op: str, payload: dict | None = None) -> str:
    """Short human label for one translated ctx.* invocation."""
    payload = payload if isinstance(payload, dict) else {}
    if not op:
        return "ctx.?"
    if op == "gui":
        goal = payload.get("goal") or payload.get("task") or ""
        success = payload.get("success")
        target = payload.get("target")
        success_text = ""
        if (
            isinstance(success, dict)
            and success.get("entity")
        ):
            entity = success.get("entity")
            fields = success.get("fields") or []
            success_text = (
                f", success=collection({entity!r}, "
                f"fields={fields!r})"
            )
        target_text = f", target={target!r}" if target is not None else ""
        result_text = (
            f" → {payload.get('produced_state')}"
            if payload.get("produced_state")
            else ""
        )
        if goal:
            return (
                f"ctx.gui({goal!r}{success_text}{target_text})"
                f"{result_text}"
            )
        return "ctx.gui(…)"
    if op == "write":
        task = payload.get("task") or ""
        return f"ctx.write({task!r}, …)" if task else "ctx.write(…)"
    if op == "lookup":
        state = payload.get("state")
        entity = payload.get("entity") or "?"
        filters = payload.get("filters") or {}
        fields = payload.get("required_fields") or payload.get("fields") or []
        bits = [
            *([f"state={state}"] if state else []),
            f"entity={entity!r}",
        ]
        if filters:
            bits.append(f"filters={filters!r}")
        if fields:
            bits.append(f"fields={list(fields)[:6]!r}")
        return f"ctx.lookup({', '.join(bits)})"
    if op == "constrain":
        state = payload.get("state")
        entity = payload.get("entity") or "?"
        filters = payload.get("filters") or {}
        state_text = f"{state}, " if state else ""
        return f"ctx.constrain({state_text}{entity!r}, {filters!r})"
    if op == "acquire":
        state = payload.get("state")
        entity = payload.get("entity") or "?"
        fields = payload.get("fields") or []
        state_text = f"{state}, " if state else ""
        return (
            f"ctx.acquire({state_text}{entity!r}, "
            f"fields={list(fields)[:6]!r})"
        )
    if op == "read":
        state = payload.get("state")
        fields = payload.get("fields") or []
        state_text = f"{state}, " if state else ""
        return f"ctx.read({state_text}fields={list(fields)[:8]!r})"
    if op == "focus":
        state = payload.get("state")
        fields = payload.get("fields") or []
        state_text = f"{state}, " if state else ""
        return f"ctx.focus({state_text}fields={list(fields)[:6]!r})"
    if op == "command":
        cap = payload.get("capability") or "?"
        return f"ctx.command({cap!r})"
    if op == "query":
        entity = payload.get("entity") or "?"
        return f"ctx.query({entity!r}, …)"
    return f"ctx.{op}(…)"


_CODING_CTX_PLAN_OPS = frozenset({
    "gui", "write", "query", "lookup", "constrain", "focus",
    "acquire", "read", "command", "interact",
})

# Plan-level ctx.* call → ordered runtime ops it may expand into.
_PLAN_RUNTIME_CONSUME: dict[str, tuple[str, ...]] = {
    "gui": ("gui",),
    "write": ("write",),
    "query": ("lookup", "constrain", "acquire"),
    "lookup": ("lookup",),
    "constrain": ("constrain",),
    "acquire": ("acquire",),
    "read": ("focus", "read"),
    "focus": ("focus",),
    "command": ("command",),
    "interact": ("gui", "write"),
}


def _coding_plan_call_sites(source: str) -> list[dict]:
    """Static ctx.* call sites from source, source order (by lineno)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    sites: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "ctx"
            and func.attr in _CODING_CTX_PLAN_OPS
        ):
            continue
        sites.append({
            "op": func.attr,
            "lineno": int(getattr(node, "lineno", 0) or 0),
            "end_lineno": int(
                getattr(node, "end_lineno", None)
                or getattr(node, "lineno", 0)
                or 0
            ),
        })
    sites.sort(key=lambda item: (item["lineno"], item["op"]))
    return sites


def _coding_runtime_calls(orchestrator: dict) -> list[dict]:
    """Normalized runtime statement list from report_run_log."""
    out: list[dict] = []
    for index, entry in enumerate(orchestrator.get("report_run_log") or [], 1):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        payload = (
            dict(entry["coding_payload"])
            if isinstance(entry.get("coding_payload"), dict)
            else {}
        )
        op = _infer_coding_op(
            coding_op=str(entry.get("coding_op") or ""),
            executor=str(entry.get("executor") or ""),
            inputs=payload,
            name=name,
        )
        if op == "gui" and not payload.get("task"):
            payload = {**payload, "task": name}
        if op == "lookup" and not payload.get("entity") and "collection" in name:
            m = re.search(r"collection '([^']+)'", name)
            if m:
                payload = {**payload, "entity": m.group(1)}
            fm = re.search(r"filters=(\{.*?\})", name)
            if fm:
                try:
                    payload = {**payload, "filters": ast.literal_eval(fm.group(1))}
                except Exception:  # noqa: BLE001
                    pass
            rm = re.search(r"fields=(\[.*?\])", name)
            if rm:
                try:
                    payload = {
                        **payload,
                        "required_fields": ast.literal_eval(rm.group(1)),
                    }
                except Exception:  # noqa: BLE001
                    pass
        plan = str(entry.get("coding_plan") or "")
        plan_step = int(entry.get("coding_plan_step") or 0)
        plan_steps = int(entry.get("coding_plan_steps") or 0)
        out.append({
            "ordinal": index,
            "sid": _coding_statement_id(entry),
            "op": op,
            "phase": str(result.get("phase") or ""),
            "payload": payload,
            "name": name,
            "plan_op": plan,
            "plan_step": plan_step,
            "plan_steps": plan_steps,
        })
    return out


def _designed_plan_steps(plan_op: str, matched: list[dict]) -> int:
    """How many internal statements a plan-level API is designed to emit."""
    if plan_op == "query":
        return 3
    if plan_op == "read":
        if any(str(c.get("op") or "") == "focus" for c in matched):
            return 2
        return 1
    return max(1, len(matched) or 1)


def _apply_plan_expansion_to_group(plan_op: str, matched: list[dict]) -> None:
    """Mutate matched runtime calls with plan_op / step / siblings for reporting."""
    if not matched:
        return
    designed = _designed_plan_steps(plan_op, matched)
    siblings = [int(c.get("ordinal") or 0) for c in matched]
    pending_ops: list[str] = []
    if plan_op == "query":
        for pending_op in ("lookup", "constrain", "acquire"):
            if not any(c.get("op") == pending_op for c in matched):
                pending_ops.append(pending_op)
    if plan_op == "read" and designed == 2 and not any(c.get("op") == "read" for c in matched):
        pending_ops.append("read")
    for index, call in enumerate(matched, 1):
        # Prefer runtime-recorded plan meta when present.
        if not call.get("plan_op"):
            call["plan_op"] = plan_op
        if not call.get("plan_step"):
            call["plan_step"] = index
        if not call.get("plan_steps"):
            call["plan_steps"] = designed
        call["plan_siblings"] = siblings
        call["plan_pending"] = list(pending_ops)


def _match_runtime_to_plan_sites(
    plan_sites: list[dict],
    runtime_calls: list[dict],
) -> tuple[dict[int, list[dict]], list[dict]]:
    """Map plan-source linenos → runtime calls; leftover runtime calls returned separately."""
    by_line: dict[int, list[dict]] = {}
    queue = list(runtime_calls)
    for site in plan_sites:
        plan_op = str(site.get("op") or "")
        lineno = int(site.get("lineno") or 0)
        if not lineno or not queue:
            continue
        allowed = _PLAN_RUNTIME_CONSUME.get(plan_op, (plan_op,))
        matched: list[dict] = []
        # Consume a contiguous prefix of allowed runtime ops (macro expansion).
        while queue and queue[0].get("op") in allowed:
            matched.append(queue.pop(0))
            # gui/write/command: exactly one
            if plan_op in {"gui", "write", "command", "lookup", "constrain", "acquire", "focus"}:
                break
            # query: stop after acquire if present, else keep taking its ordered phases
            if plan_op == "query" and matched[-1].get("op") == "acquire":
                break
            # read: stop after read
            if plan_op == "read" and matched[-1].get("op") == "read":
                break
        if matched:
            _apply_plan_expansion_to_group(plan_op, matched)
            by_line.setdefault(lineno, []).extend(matched)
    return by_line, queue


def _enrich_runtime_plan_expansion(
    runtime_calls: list[dict],
    *,
    source: str = "",
) -> dict[str, dict]:
    """Return sid → expansion meta; also mutates runtime_calls in place."""
    if not runtime_calls:
        return {}

    plan_sites = _coding_plan_call_sites(source) if source else []
    if plan_sites:
        _by_line, leftovers = _match_runtime_to_plan_sites(
            plan_sites, list(runtime_calls),
        )
        for call in leftovers:
            plan = str(call.get("plan_op") or call.get("op") or "")
            if plan == "lookup" or call.get("op") == "lookup":
                plan = str(call.get("plan_op") or "query")
            _apply_plan_expansion_to_group(plan or "gui", [call])
    else:
        # No source: group by recorded plan tags, else query phases by order.
        i = 0
        while i < len(runtime_calls):
            call = runtime_calls[i]
            plan = str(call.get("plan_op") or "")
            if plan and int(call.get("plan_steps") or 0) > 1:
                group = [call]
                j = i + 1
                while (
                    j < len(runtime_calls)
                    and str(runtime_calls[j].get("plan_op") or "") == plan
                ):
                    group.append(runtime_calls[j])
                    j += 1
                _apply_plan_expansion_to_group(plan, group)
                i = j
                continue
            if call.get("op") == "lookup":
                group = [call]
                j = i + 1
                while (
                    j < len(runtime_calls)
                    and runtime_calls[j].get("op") in {"constrain", "acquire"}
                ):
                    group.append(runtime_calls[j])
                    if runtime_calls[j].get("op") == "acquire":
                        j += 1
                        break
                    j += 1
                _apply_plan_expansion_to_group("query", group)
                i = j
                continue
            else:
                _apply_plan_expansion_to_group(
                    str(call.get("plan_op") or call.get("op") or "gui"), [call],
                )
            i += 1

    by_sid: dict[str, dict] = {}
    for call in runtime_calls:
        sid = str(call.get("sid") or "")
        if not sid:
            continue
        plan_op = str(call.get("plan_op") or call.get("op") or "")
        step = int(call.get("plan_step") or 1)
        steps = int(call.get("plan_steps") or 1)
        by_sid[sid] = {
            "plan_op": plan_op,
            "plan_step": step,
            "plan_steps": steps,
            "op": str(call.get("op") or ""),
            "ordinal": int(call.get("ordinal") or 0),
            "siblings": list(call.get("plan_siblings") or [call.get("ordinal")]),
            "pending": list(call.get("plan_pending") or []),
            "expanded": bool(steps > 1 or plan_op in {"query", "read"} and plan_op != call.get("op")),
            "label": _plan_step_label(plan_op, str(call.get("op") or ""), step, steps),
        }
    return by_sid


def _plan_step_label(plan_op: str, step_op: str, step: int, steps: int) -> str:
    """Human label: ctx.query · 步骤 1/3 · lookup"""
    plan_op = plan_op or step_op or "?"
    step_op = step_op or "?"
    if steps > 1 or plan_op != step_op:
        return f"ctx.{plan_op} · 步骤 {step}/{steps} · {step_op}"
    return f"ctx.{plan_op}"


def _plan_is_expanded(plan_meta: dict | None, step_op: str = "") -> bool:
    if not isinstance(plan_meta, dict) or not plan_meta:
        return False
    plan_op = str(plan_meta.get("plan_op") or "")
    steps = int(plan_meta.get("plan_steps") or 0)
    op = step_op or str(plan_meta.get("op") or "")
    return bool(plan_op and (steps > 1 or (op and plan_op != op)))


def _macro_failure_verdict(
    plan_meta: dict | None,
    *,
    phase: str = "",
    step_op: str = "",
) -> str:
    """One-line diagnosis when a plan-level API dies mid-expansion."""
    if phase not in {"exhausted", "failed", "stopped", "interrupted"}:
        return ""
    if not _plan_is_expanded(plan_meta, step_op):
        return ""
    assert isinstance(plan_meta, dict)
    plan_op = str(plan_meta.get("plan_op") or "?")
    step = int(plan_meta.get("plan_step") or 1)
    steps = int(plan_meta.get("plan_steps") or 1)
    op = step_op or str(plan_meta.get("op") or "?")
    pending = [str(p) for p in (plan_meta.get("pending") or []) if p]
    text = f"ctx.{plan_op} 在步骤 {step}/{steps}（{op}）失败"
    if pending:
        text += "，" + "、".join(pending) + " 未执行"
    elif step < steps:
        text += f"，后续步骤未执行"
    return text


def _render_runtime_ann_chips(calls: list[dict]) -> str:
    chips: list[str] = []
    plan_op = ""
    plan_steps = 0
    if calls:
        plan_op = str(calls[0].get("plan_op") or "")
        plan_steps = int(calls[0].get("plan_steps") or 0)
    if plan_op and plan_steps > 1:
        chips.append(
            f'<span class="coding-src-chip coding-src-chip-plan">'
            f'<span class="coding-src-chip-op">ctx.{_safe(plan_op)}</span>'
            f'<span class="coding-src-chip-meta">{len(calls)}/{plan_steps} 步</span>'
            f'</span>'
        )
    for call in calls:
        ordinal = int(call.get("ordinal") or 0)
        sid = str(call.get("sid") or "")
        phase = str(call.get("phase") or "")
        phase_cls = {
            "completed": "coding-phase-ok",
            "exhausted": "coding-phase-fail",
            "failed": "coding-phase-fail",
        }.get(phase, "coding-phase-warn")
        phase_html = (
            f'<span class="coding-phase {phase_cls}">{_safe(phase)}</span>'
            if phase else ""
        )
        op = str(call.get("op") or "")
        step = int(call.get("plan_step") or 0)
        steps = int(call.get("plan_steps") or 0)
        step_meta = f"{step}/{steps}" if steps > 1 else ""
        title = _plan_step_label(
            str(call.get("plan_op") or ""), op, step or 1, steps or 1,
        )
        link = (
            f'<a class="coding-call-link" href="#ms-{_safe(sid)}" title="{_safe(title)}">'
            f'#{ordinal}</a>'
            if sid else f'<span class="coding-call-link">#{ordinal}</span>'
        )
        chips.append(
            f'<span class="coding-src-chip">{link}'
            f'<span class="coding-src-chip-op">{_safe(op or "?")}'
            f'{(" · " + step_meta) if step_meta else ""}</span>'
            f'{phase_html}</span>'
        )
    pending = list(calls[0].get("plan_pending") or []) if calls else []
    for pending_op in pending:
        chips.append(
            f'<span class="coding-src-chip coding-src-chip-pending">'
            f'<span class="coding-src-chip-op">{_safe(pending_op)} 未执行</span>'
            f'</span>'
        )
    return "".join(chips)


def _render_annotated_coding_source(source: str, orchestrator: dict) -> str:
    """Python source with runtime call annotations on matching ctx.* lines."""
    lines = source.splitlines()
    plan_sites = _coding_plan_call_sites(source)
    runtime_calls = _coding_runtime_calls(orchestrator)
    # Enrich plan expansion (query→lookup+constrain+acquire) before source alignment.
    _enrich_runtime_plan_expansion(runtime_calls, source=source)
    by_line, leftovers = _match_runtime_to_plan_sites(
        plan_sites, list(runtime_calls),
    )

    # Fallback: no AST sites or no matches — keep chips in footer (don't mis-attach).
    if runtime_calls and not any(by_line.values()):
        leftovers = list(runtime_calls)
        by_line = {}

    row_html: list[str] = []
    for lineno, raw in enumerate(lines, 1):
        code = _safe(raw) if raw else " "
        ann = _render_runtime_ann_chips(by_line.get(lineno) or [])
        ann_html = f'<span class="coding-src-ann">{ann}</span>' if ann else ""
        has_ann = " coding-src-line-hit" if ann else ""
        row_html.append(
            f'<div class="coding-src-line{has_ann}">'
            f'<span class="coding-src-ln">{lineno}</span>'
            f'<span class="coding-src-code">{code}</span>'
            f'{ann_html}'
            f'</div>'
        )

    footer = ""
    if leftovers:
        footer = (
            '<div class="coding-src-footer">'
            '<span class="coding-src-footer-label">未对齐到源码行的运行调用</span>'
            f'{_render_runtime_ann_chips(leftovers)}'
            '</div>'
        )
    note = (
        '<div class="coding-src-legend">'
        '行尾：计划 API 展开的 Statement 步骤（如 query→lookup+constrain+acquire），'
        '点击 #N 跳转卡片'
        '</div>'
    )
    return (
        f'{note}'
        f'<div class="coding-source coding-source-annotated">'
        f'{"".join(row_html)}'
        f'{footer}'
        f'</div>'
    )


def coding_plan_expansion_by_sid(orchestrator: dict | None) -> dict[str, dict]:
    """Public helper: statement id → plan expansion meta for card headers/data panels."""
    if not orchestrator:
        return {}
    source = str((orchestrator.get("program") or {}).get("source") or "")
    calls = _coding_runtime_calls(orchestrator)
    return _enrich_runtime_plan_expansion(calls, source=source)


def _render_coding_program_shell(orchestrator: dict, program: dict) -> str:
    """Coding plan shell: goal, review, annotated Python source."""
    source = str(program.get("source") or "").strip()
    if not source:
        return ""
    goal = str(program.get("goal") or "")
    input_html = (
        f'<div class="prog-input"><span class="prog-input-label">输入</span>{_safe(goal)}'
        '<span class="prog-input-arrow">↓ Statement 卡片（数据 + UI 交互）</span></div>'
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
    runtime_n = len(_coding_runtime_calls(orchestrator))
    review_html = (
        '<div class="compat-row">'
        f'<span class="compat-chip">{_safe(review_label)}</span>'
        f'<span class="coding-note">'
        f'源码行尾标注 {runtime_n} 次运行调用 · 点击跳转卡片'
        f'</span>'
        '</div>'
    )
    # Open source by default when there are runtime annotations to show.
    open_attr = " open" if runtime_n else ""
    source_html = (
        f'<details class="coding-source-wrap"{open_attr}>'
        f'<summary>完整 Python 源码'
        f'<span class="coding-note"> · 运行调用已标注在对应行</span></summary>'
        f'{_render_annotated_coding_source(source, orchestrator)}'
        f'</details>'
    )
    return (
        '<div class="statement prog-section" id="ms-orchestrate-coding">'
        '<div class="statement-header">'
        '<h2>#0</h2>'
        '<span class="statement-name">Coding Orchestrator · Python 执行计划</span>'
        '<span class="statement-badge statement-badge-default">python</span>'
        f'{_render_orchestrator_metrics(orchestrator)}'
        '</div>'
        f'<div class="prog-body">{input_html}{review_html}{source_html}'
        f'{_render_orchestrator_context_reports(orchestrator)}'
        '</div>'
        '</div>'
    )


def _build_statement_call_params(
    *,
    name: str = "",
    success: str = "",
    executor: str = "",
    inputs: dict | None = None,
    coding_op: str = "",
    coding_payload: dict | None = None,
    call: dict | None = None,
) -> dict:
    """Assemble the full statement-executor call parameter object for the data panel."""
    call = dict(call) if isinstance(call, dict) else {}
    inputs = inputs if isinstance(inputs, dict) else {}
    coding_payload = dict(coding_payload) if isinstance(coding_payload, dict) else {}

    op = _infer_coding_op(
        coding_op=coding_op,
        executor=executor or str(call.get("executor") or ""),
        inputs=coding_payload or inputs or (
            call.get("inputs") if isinstance(call.get("inputs"), dict) else {}
        ),
        name=name or str(call.get("goal") or ""),
    )
    flat = _flatten_coding_inputs(
        coding_op=op,
        coding_payload=coding_payload or None,
        inputs=inputs or (
            call.get("inputs") if isinstance(call.get("inputs"), dict) else {}
        ),
    )
    if op == "gui" and not flat.get("task"):
        task = name or str(call.get("goal") or "")
        if task:
            flat = {**flat, "task": task}

    params: dict = {}
    if op:
        params["ctx_op"] = op
    # Prefer explicit coding payload when present (new runs); else flattened request.
    if coding_payload:
        params["ctx_payload"] = coding_payload
    elif flat:
        params["ctx_payload"] = flat

    # Full statement-executor contract (what the runtime actually dispatched).
    executor_call: dict = {}
    for key in (
        "id",
        "executor",
        "goal",
        "success",
        "on",
        "scope",
        "persistence",
        "inputs",
        "required_values",
        "observe_fields",
        "returns",
        "args",
        "capability",
        "required_fields",
        "reads",
        "coverage",
    ):
        if key in call and call.get(key) is not None:
            executor_call[key] = call[key]
    # Fill gaps from page-level fields when journal call snapshot is partial.
    if executor and "executor" not in executor_call:
        executor_call["executor"] = executor
    if name and "goal" not in executor_call:
        executor_call["goal"] = name
    if success and "success" not in executor_call:
        executor_call["success"] = success
    if inputs and "inputs" not in executor_call:
        executor_call["inputs"] = inputs
    if executor_call:
        params["statement"] = executor_call
    return params


def _coding_json_pre(value: object, *, limit: int = 8000) -> str:
    """Light-theme JSON/pre body for data panels (always the same surface)."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        text = text[: limit - 3] + "…"
    return f'<pre class="coding-data-pre">{_safe(text)}</pre>'


def _coding_arg_row(key: str, value: object) -> str:
    """One key/value row; scalars as value text, structures as light pre."""
    if value is None or isinstance(value, (bool, int, float)):
        body = f'<span class="coding-data-val">{_safe(str(value))}</span>'
    elif isinstance(value, str):
        body = f'<span class="coding-data-val">{_safe(value)}</span>'
    else:
        body = _coding_json_pre(value, limit=2400)
    return (
        f'<div class="coding-data-row">'
        f'<span class="coding-data-key">{_safe(key)}</span>'
        f'{body}'
        f'</div>'
    )


def _coding_details(title: str, body: str, *, plan: bool = False) -> str:
    """Render one consistently collapsed statement-data section."""
    block_class = " coding-data-block-plan" if plan else ""
    details_class = " coding-plan-details" if plan else ""
    return (
        f'<div class="coding-data-block{block_class}">'
        f'<details class="coding-data-details{details_class}">'
        f'<summary>{_safe(title)}</summary>'
        f'<div class="coding-data-details-body">{body}</div>'
        f'</details></div>'
    )


# Preferred field order for ctx payload rows — matches common call signatures.
_CTX_PAYLOAD_KEY_ORDER: dict[str, tuple[str, ...]] = {
    "gui": ("task", "target"),
    "write": ("task", "target", "values"),
    "lookup": ("entity", "field", "fallback", "filters", "required_fields", "fields"),
    "constrain": ("entity", "filters"),
    "acquire": ("entity", "fields", "coverage", "scope"),
    "read": ("fields", "target"),
    "focus": ("target", "fields"),
    "command": ("capability",),
    "query": ("entity", "fields", "filters", "field", "fallback", "coverage"),
}


def _ordered_payload_items(op: str, payload: dict) -> list[tuple[str, object]]:
    """Stable, signature-first key order for readable call-arg rows."""
    preferred = _CTX_PAYLOAD_KEY_ORDER.get(op, ())
    seen: set[str] = set()
    items: list[tuple[str, object]] = []
    for key in preferred:
        if key in payload:
            items.append((key, payload[key]))
            seen.add(key)
    for key, value in payload.items():
        if key not in seen:
            items.append((str(key), value))
    return items


def _render_coding_data_panel(
    *,
    name: str = "",
    success: str = "",
    result: dict | None = None,
    outputs: dict | None = None,
    inputs: dict | None = None,
    coding_op: str = "",
    coding_payload: dict | None = None,
    executor: str = "",
    call: dict | None = None,
    plan_meta: dict | None = None,
    omit_call_label: bool = False,
) -> str:
    """Statement-local data: readable call args (light) + run result."""
    result = result if isinstance(result, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    if not outputs:
        raw_out = result.get("outputs")
        if isinstance(raw_out, dict):
            outputs = raw_out

    params = _build_statement_call_params(
        name=name,
        success=success,
        executor=executor,
        inputs=inputs,
        coding_op=coding_op,
        coding_payload=coding_payload,
        call=call,
    )
    op = str(params.get("ctx_op") or "")
    payload = params.get("ctx_payload") if isinstance(params.get("ctx_payload"), dict) else {}
    statement = params.get("statement") if isinstance(params.get("statement"), dict) else {}
    plan_meta = plan_meta if isinstance(plan_meta, dict) else {}

    sections: list[str] = []

    phase = str(result.get("phase") or "")
    plan_expanded = _plan_is_expanded(plan_meta, op)
    plan_op = str(plan_meta.get("plan_op") or "")
    plan_step = int(plan_meta.get("plan_step") or 0)
    plan_steps = int(plan_meta.get("plan_steps") or 0)

    # ── 0) 宏失败结论：抬到最前，避免先翻参数 ──
    verdict = _macro_failure_verdict(plan_meta, phase=phase, step_op=op)
    if verdict:
        sections.append(
            f'<div class="coding-macro-verdict">{_safe(verdict)}</div>'
        )

    # ── 1a) 计划展开：与其他数据区块一致，默认折叠 ──
    if plan_expanded:
        plan_rows: list[str] = [
            _coding_arg_row("API", f"ctx.{plan_op}"),
            _coding_arg_row("本步", f"{plan_step}/{plan_steps} · {op or '?'}"),
        ]
        siblings = [n for n in (plan_meta.get("siblings") or []) if n]
        if siblings:
            plan_rows.append(
                _coding_arg_row("关联卡片", " ".join(f"#{n}" for n in siblings)),
            )
        pending = plan_meta.get("pending") or []
        if pending:
            plan_rows.append(
                _coding_arg_row(
                    "未执行",
                    ", ".join(
                        f"ctx.{p}" if not str(p).startswith("ctx.") else str(p)
                        for p in pending
                    ),
                ),
            )
        summary = (
            f"计划展开 · ctx.{plan_op} 步骤 {plan_step}/{plan_steps}"
            if plan_op else "计划展开"
        )
        sections.append(_coding_details(summary, "".join(plan_rows), plan=True))

    # ── 1b) 本步参数：当前 Statement 内部 op 的入参（不再重复 op/签名）──
    step_rows: list[str] = []
    if op and not omit_call_label:
        step_rows.append(
            f'<div class="coding-data-row">'
            f'<span class="coding-data-key">调用</span>'
            f'<span class="coding-data-val"><code>{_safe(_coding_call_label(op, payload))}</code></span>'
            f'</div>'
        )
    for field, value in _ordered_payload_items(op, payload)[:24]:
        if value is None:
            continue
        # Skip empty optional strings that only clutter the form (e.g. fallback="").
        if value == "" or value == [] or value == {}:
            continue
        step_rows.append(_coding_arg_row(str(field), value))
    if step_rows:
        step_title = "本步参数" if plan_expanded else "调用参数"
        sections.append(_coding_details(step_title, "".join(step_rows)))

    # ── 2) 运行结果（failure 与顶部宏结论重复时，只保留 evidence 等补充信息）──
    result_rows: list[str] = []
    if phase and not verdict:
        result_rows.append(_coding_arg_row("phase", phase))
    summary = str(result.get("summary") or "")
    failure = str(result.get("failure_evidence") or "")
    # When macro verdict already names the failure locus, skip duplicating the same text.
    if summary and (not verdict or summary.strip() not in verdict):
        # Also skip if summary == failure and we'll show failure once.
        if not (failure and summary.strip() == failure.strip() and verdict):
            result_rows.append(_coding_arg_row("summary", summary))
    if failure and not verdict:
        result_rows.append(
            f'<div class="coding-data-row coding-data-fail">'
            f'<span class="coding-data-key">failure</span>'
            f'<span class="coding-data-val">{_safe(failure)}</span></div>'
        )
    elif failure and verdict and failure.strip() not in verdict and (
        not summary or failure.strip() != summary.strip()
    ):
        result_rows.append(
            f'<div class="coding-data-row coding-data-fail">'
            f'<span class="coding-data-key">detail</span>'
            f'<span class="coding-data-val">{_safe(failure)}</span></div>'
        )
    evidence = result.get("evidence") or []
    if isinstance(evidence, list) and evidence:
        for index, item in enumerate(evidence[:6], 1):
            if not item:
                continue
            result_rows.append(_coding_arg_row(f"ev.{index}", str(item)))
    if outputs:
        if len(json.dumps(outputs, ensure_ascii=False, default=str)) > 6000:
            out_body = _report_value(outputs, sample_items=3, limit=2400)
            result_rows.append(
                f'<div class="coding-data-row">'
                f'<span class="coding-data-key">outputs</span>'
                f'{_coding_json_pre(out_body)}'
                f'</div>'
            )
        else:
            result_rows.append(
                f'<div class="coding-data-row">'
                f'<span class="coding-data-key">outputs</span>'
                f'{_coding_json_pre(outputs)}'
                f'</div>'
            )
    if result_rows:
        sections.append(_coding_details("运行结果", "".join(result_rows)))

    # ── 3) Statement 契约（低频细节，放最后）──
    if statement:
        sections.append(_coding_details(
            "Statement 执行器契约",
            _coding_json_pre(statement),
        ))

    if not sections:
        if name:
            return (
                f'<div class="coding-stmt-data">'
                f'{_coding_details("数据", _coding_arg_row("goal", name))}'
                f'</div>'
            )
        return ""
    return f'<div class="coding-stmt-data">{"".join(sections)}</div>'


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
