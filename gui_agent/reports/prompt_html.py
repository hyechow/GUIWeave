"""HTML rendering for model input/output diagnostics."""

from __future__ import annotations

import json
import re

from gui_agent.core.config import pricing_currency

from .html_utils import _attr, _safe
from .metrics import _fmt_tokens, _sum_tokens, _token_cost


def _render_module_io_html(reports: list[dict], token_usage: dict | None = None) -> str:
    calls = _collect_module_calls(reports)
    if not calls:
        if reports:
            return '<div class="compat-row"><span class="compat-chip">本次运行未记录 prompt snapshot</span></div>'
        return ""

    token_usage = token_usage if isinstance(token_usage, dict) else {}
    token_summary = _render_token_usage_html(token_usage)
    detail_meta = _module_io_meta(calls, token_usage)
    calls_html = []
    for idx, call in enumerate(calls, start=1):
        label_text = str(call.get("label") or f"module {idx}")
        label = _safe(label_text)
        snapshot = call.get("prompt") if isinstance(call.get("prompt"), dict) else {}
        output = call.get("output") if isinstance(call.get("output"), dict) else {}
        roles_html = []
        for role_block in snapshot.get("roles") or []:
            if not isinstance(role_block, dict):
                continue
            role = _safe(str(role_block.get("role") or "message"))
            parts_html = []
            for part_i, part in enumerate(role_block.get("parts") or [], start=1):
                if not isinstance(part, dict):
                    continue
                parts_html.append(_render_prompt_part(part, part_i, output))
            roles_html.append(
                f'<section class="prompt-role">'
                f'<div class="prompt-role-title">{role}</div>'
                f'{"".join(parts_html)}'
                f'</section>'
            )
        output_html = _render_output_html(output)
        summary = _render_call_summary(label_text, output)
        search_index = _attr(
            " ".join(
                bit
                for bit in [
                    label_text,
                    str(output.get("schema") or ""),
                    _call_summary_text(label_text, output),
                ]
                if bit
            )
        )
        calls_html.append(
            f'<details class="prompt-call" data-search-index="{search_index}">'
            f'<summary><span class="prompt-call-title">{idx}. {label}</span>{summary}</summary>'
            f'<div class="prompt-call-body">{"".join(roles_html)}{output_html}</div>'
            f'</details>'
        )

    return (
        f'<details class="prompt-detail">'
        f'<summary class="prompt-detail-head">'
        f'<span class="prompt-detail-title">模型调用详情</span>'
        f'<span class="prompt-detail-meta">{_safe(detail_meta)}</span>'
        f'</summary>'
        f'<div class="prompt-list">{token_summary}{"".join(calls_html)}</div>'
        f'</details>'
    )


def _render_prompt_part(part: dict, part_i: int, output: dict) -> str:
    label_raw = str(part.get("label") or f"part-{part_i}")
    label_text = _safe(label_raw)
    source = _safe(str(part.get("source") or ""))
    source_type = _safe(str(part.get("source_type") or part.get("type") or ""))
    ttl = _safe(str(part.get("ttl") or ""))
    budget = _safe(str(part.get("budget") or ""))
    chars = int(part.get("chars") or len(str(part.get("text") or "")))
    meta = []
    if source_type:
        meta.append(f"type={source_type}")
    if source:
        meta.append(f"source={source}")
    if ttl:
        meta.append(f"ttl={ttl}")
    if budget:
        meta.append(f"budget={budget}")
    meta.append(f"{chars} chars")
    text_raw = str(part.get("text") or "")
    text = _safe(text_raw)
    pre_cls = "prompt-pre prompt-pre-image" if part.get("type") == "image" else "prompt-pre"
    search_index = _attr(
        " ".join([label_raw, str(part.get("source") or ""), str(part.get("source_type") or "")])
    )
    if label_raw == "schema_instruction":
        summary = _safe(_schema_instruction_summary(text_raw, output))
        return (
            f'<details class="prompt-part prompt-schema" data-search-index="{search_index}">'
            f'<summary class="prompt-part-head">'
            f'<span class="prompt-part-no">{part_i}</span>'
            f'<span class="prompt-part-label">{summary}</span>'
            f'<span class="prompt-part-meta">完整 schema 默认折叠 · {" · ".join(meta)}</span>'
            f'</summary>'
            f'<pre class="{pre_cls}">{text}</pre>'
            f'</details>'
        )
    return (
        f'<details class="prompt-part prompt-part-collapsed" data-search-index="{search_index}">'
        f'<summary class="prompt-part-head">'
        f'<span class="prompt-part-no">{part_i}</span>'
        f'<span class="prompt-part-label">{label_text}</span>'
        f'<span class="prompt-part-meta">{_safe(" · ".join(meta))}</span>'
        f'</summary>'
        f'<pre class="{pre_cls}">{text}</pre>'
        f'</details>'
    )


def _render_token_usage_html(token_usage: dict) -> str:
    ti, to = _sum_tokens(token_usage)
    if not ti and not to:
        return ""
    rows: list[str] = []
    for name, usage in token_usage.items():
        if not isinstance(usage, dict):
            continue
        mi = int(usage.get("input") or 0)
        mo = int(usage.get("output") or 0)
        if not mi and not mo:
            continue
        rows.append(
            f'<span class="prompt-token-chip">'
            f'<span class="prompt-token-name">{_safe(str(name))}</span>'
            f'<span class="prompt-token-count">{_fmt_tokens(mi)}/{_fmt_tokens(mo)} tok</span>'
            f'</span>'
        )
    if not rows:
        return ""
    count_text = f"{len(rows)} module" if len(rows) == 1 else f"{len(rows)} modules"
    return (
        f'<details class="prompt-token-detail">'
        f'<summary>'
        f'<span>Token 明细</span>'
        f'<span class="prompt-token-summary">{count_text}</span>'
        f'</summary>'
        f'<div class="prompt-token-row">{"".join(rows)}</div>'
        f'</details>'
    )


def _token_usage_summary_text(token_usage: dict) -> str:
    ti, to = _sum_tokens(token_usage)
    if not ti and not to:
        return ""
    return f"{_fmt_tokens(ti)}/{_fmt_tokens(to)} tok · ≈{pricing_currency()}{_token_cost(token_usage):.4f}"


def _module_io_meta(calls: list[dict], token_usage: dict) -> str:
    call_text = f"{len(calls)} call" if len(calls) == 1 else f"{len(calls)} calls"
    token_text = _token_usage_summary_text(token_usage)
    if token_text:
        return f"{call_text} · {token_text}"
    labels = ", ".join(str(call.get("label") or "module") for call in calls[:4])
    if len(calls) > 4:
        labels += " ..."
    return f"{call_text} · {labels}" if labels else call_text


def _schema_instruction_summary(text: str, output: dict) -> str:
    schema = str(output.get("schema") or "") if isinstance(output, dict) else ""
    if not schema:
        m = re.search(r'"title"\s*:\s*"([^"]+)"', text)
        schema = m.group(1) if m else "schema"
    required = _count_schema_line(text, "顶层必填字段")
    optional = _count_schema_line(text, "顶层可选字段")
    return f"schema_instruction · {schema} · {required} required / {optional} optional"


def _count_schema_line(text: str, label: str) -> int:
    m = re.search(rf"{re.escape(label)}[:：]\s*([^\n]+)", text)
    if not m:
        return 0
    body = m.group(1).strip()
    if not body or body in {"无", "none", "None", "[]"}:
        return 0
    return len([p for p in re.split(r"[,，、]\s*", body) if p.strip()])


def _collect_module_calls(reports: list[dict]) -> list[dict]:
    calls: list[dict] = []
    for report in reports or []:
        if not isinstance(report, dict):
            continue
        kind = report.get("kind")
        label = str(report.get("label") or "")
        if kind == "prompt_snapshot":
            calls.append({"label": label, "prompt": report, "output": None})
        elif kind == "llm_output":
            target = next(
                (
                    call for call in calls
                    if call.get("label") == label and not call.get("output")
                ),
                None,
            )
            if target is None:
                calls.append({"label": label, "prompt": None, "output": report})
            else:
                target["output"] = report
    return calls


def _render_call_summary(label: str, output: dict) -> str:
    text = _call_summary_text(label, output)
    if not text:
        return '<span class="prompt-call-summary prompt-call-summary-muted">无输出摘要</span>'
    cls = "prompt-call-summary"
    parsed = output.get("parsed") if isinstance(output.get("parsed"), dict) else {}
    status = str(parsed.get("status") or "").lower() if parsed else ""
    if status in {"done", "success", "completed"}:
        cls += " prompt-call-summary-ok"
    elif status in {"in_progress", "pending"}:
        cls += " prompt-call-summary-warn"
    elif status in {"failed", "error"}:
        cls += " prompt-call-summary-error"
    return f'<span class="{cls}">{_safe(text)}</span>'


def _call_summary_text(label: str, output: dict) -> str:
    if not output:
        return ""
    parsed = output.get("parsed")
    if not isinstance(parsed, dict):
        return _shorten(str(output.get("raw_output") or ""))
    lname = label.lower()
    if "transition" in lname:
        kind = str(parsed.get("kind") or "unknown")
        reason = str(parsed.get("reason") or parsed.get("summary") or "")
        return _join_bits([kind, _shorten(reason, 110)])
    if "action_policy" in lname:
        action = parsed.get("action") if isinstance(parsed.get("action"), dict) else parsed
        atype = str(action.get("action_type") or action.get("type") or "action")
        xy = ""
        if action.get("x") is not None and action.get("y") is not None:
            try:
                xy = f"({float(action.get('x')):.0f},{float(action.get('y')):.0f})"
            except (TypeError, ValueError):
                xy = f"({action.get('x')},{action.get('y')})"
        target = str(action.get("target_area") or action.get("description") or action.get("text") or "")
        return _join_bits([atype, xy, _shorten(target, 90)])
    if "structured_read" in lname:
        reads = parsed.get("reads") if isinstance(parsed.get("reads"), list) else []
        bits = []
        for item in reads:
            if isinstance(item, dict):
                field = str(item.get("field") or "")
                value = str(item.get("value") or "")
                if field:
                    bits.append(f"{field}={value}" if value else field)
        return _shorten(" · ".join(bits), 130) if bits else "read"
    if "decompose" in lname:
        steps = parsed.get("steps") if isinstance(parsed.get("steps"), list) else []
        typed_bits = []
        has_finish = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            op = str(step.get("op") or "")
            if op == "finish":
                has_finish = True
            if op in {"interact", "acquire", "read", "source_check", "command"}:
                bind = str(step.get("bind") or "")
                returns = [str(v) for v in (step.get("returns") or {}) if str(v)]
                if bind and returns:
                    typed_bits.append(f"{bind}.{','.join(returns)}")
                elif returns:
                    typed_bits.append(",".join(returns))
        parts = [f"{len(steps)} steps" if steps else "program"]
        parts.extend(f"outputs {bit}" for bit in typed_bits[:2])
        if has_finish:
            parts.append("finish")
        return " · ".join(parts)
    for key in ("status", "instruction", "summary", "reason", "message"):
        if parsed.get(key):
            return _shorten(str(parsed.get(key)), 130)
    return _shorten(json.dumps(parsed, ensure_ascii=False), 130)


def _join_bits(bits: list[str]) -> str:
    return " · ".join(bit for bit in bits if bit)


def _shorten(text: str, limit: int = 80) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _render_output_html(output: dict) -> str:
    if not output:
        return (
            f'<section class="prompt-role prompt-output-missing">'
            f'<div class="prompt-role-title">output</div>'
            f'<div class="prompt-empty">（此日志没有记录该模块原始输出）</div>'
            f'</section>'
        )
    raw = _safe(str(output.get("raw_output") or ""))
    parsed = output.get("parsed")
    parsed_text = ""
    if parsed not in (None, ""):
        parsed_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    schema = _safe(str(output.get("schema") or ""))
    mode = _safe(str(output.get("mode") or ""))
    raw_label = "raw_output"
    chars = int(output.get("chars") or len(str(output.get("raw_output") or "")))
    meta = " · ".join(bit for bit in [schema, mode, f"{chars} chars"] if bit)
    parsed_html = (
        f'<div class="prompt-part">'
        f'<div class="prompt-part-head">'
        f'<span class="prompt-part-label">parsed</span>'
        f'<span class="prompt-part-meta">结构化解析结果</span>'
        f'</div>'
        f'<pre class="prompt-pre">{_safe(parsed_text)}</pre>'
        f'</div>'
        if parsed_text else ""
    )
    return (
        f'<section class="prompt-role prompt-output">'
        f'<div class="prompt-role-title">output</div>'
        f'<div class="prompt-part">'
        f'<div class="prompt-part-head">'
        f'<span class="prompt-part-label">{raw_label}</span>'
        f'<span class="prompt-part-meta">{meta}</span>'
        f'</div>'
        f'<pre class="prompt-pre prompt-pre-output">{raw}</pre>'
        f'</div>'
        f'{parsed_html}'
        f'</section>'
    )


def _render_prompt_snapshots_html(reports: list[dict]) -> str:
    """Compatibility wrapper for older imports."""
    return _render_module_io_html(reports)
