"""HTML rendering for model input/output diagnostics."""

from __future__ import annotations

from .html_utils import _safe


def _render_module_io_html(reports: list[dict]) -> str:
    calls = _collect_module_calls(reports)
    if not calls:
        return ""

    labels = ", ".join(str(call.get("label") or "module") for call in calls[:4])
    if len(calls) > 4:
        labels += " ..."
    calls_html = []
    for idx, call in enumerate(calls, start=1):
        label = _safe(str(call.get("label") or f"module {idx}"))
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
                label_text = _safe(str(part.get("label") or f"part-{part_i}"))
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
                text = _safe(str(part.get("text") or ""))
                pre_cls = "prompt-pre prompt-pre-image" if part.get("type") == "image" else "prompt-pre"
                parts_html.append(
                    f'<div class="prompt-part">'
                    f'<div class="prompt-part-head">'
                    f'<span class="prompt-part-no">{part_i}</span>'
                    f'<span class="prompt-part-label">{label_text}</span>'
                    f'<span class="prompt-part-meta">{_safe(" · ".join(meta))}</span>'
                    f'</div>'
                    f'<pre class="{pre_cls}">{text}</pre>'
                    f'</div>'
                )
            roles_html.append(
                f'<section class="prompt-role">'
                f'<div class="prompt-role-title">{role}</div>'
                f'{"".join(parts_html)}'
                f'</section>'
            )
        output_html = _render_output_html(output)
        calls_html.append(
            f'<details class="prompt-call">'
            f'<summary>{idx}. {label}</summary>'
            f'<div class="prompt-call-body">{"".join(roles_html)}{output_html}</div>'
            f'</details>'
        )

    return (
        f'<details class="prompt-detail">'
        f'<summary>模型调用详情 · {_safe(labels)}</summary>'
        f'<div class="prompt-list">{"".join(calls_html)}</div>'
        f'</details>'
    )


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
        import json

        parsed_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    schema = _safe(str(output.get("schema") or ""))
    mode = _safe(str(output.get("mode") or ""))
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
        f'<span class="prompt-part-label">raw_output</span>'
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
