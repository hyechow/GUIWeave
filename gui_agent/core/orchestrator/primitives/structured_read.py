"""Structured read: extract a read statement's `returns` fields off the result frame.

The orchestrator's read primitive. A read statement's job is single-frame data extraction
(the inspect insight): read the requested fields, interpreting non-text signals (icons /
colour / position) via the app's acceptance cues (_check.md), and return {field: value}
(empty when not readable = 当没有 — never blocks the program). The interpreter then branches
on those structured values, so "the checker saw it but the output didn't know" goes away.

This runs ON the statement's done-frame (the supervisor advanced the read statement there,
i.e. when the result is visible), so the verdict is read at exactly the right moment.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import render_prompt_context
from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.orchestrator.structured_read")


def _norm_label(s: str) -> str:
    return "".join(ch for ch in (s or "").strip().lower() if ch.isalnum())


def read_spec_label_candidates(read_spec: str, field: str) -> list[str]:
    """Return explicit UI label candidates mentioned by ``read_spec`` for ``field``.

    The DSL field can be semantic (for example ``title``) while the page label is concrete
    (for example ``Summary of Review``). When the decomposer spells that mapping out in the
    read spec, DOM readers should use it before falling back to vision.
    """
    if not read_spec or not field:
        return []

    field_norm = _norm_label(field)
    contexts: list[str] = []
    for raw in re.split(r"[\n;；]+", read_spec):
        segment = raw.strip()
        if not segment:
            continue
        head, sep, tail = segment.partition("：")
        if not sep:
            head, sep, tail = segment.partition(":")
        if not sep:
            head, sep, tail = segment.partition("=")
        if sep and _norm_label(head) == field_norm:
            contexts.append(tail.strip())
            continue
        if _norm_label(segment).startswith(field_norm):
            contexts.append(segment)

    if not contexts:
        return []

    out: list[str] = []

    def add(candidate: str) -> None:
        text = re.sub(r"\s+", " ", candidate or "").strip(" `\"'「」[]()（）")
        if not text or _norm_label(text) == field_norm:
            return
        if text not in out:
            out.append(text)

    label_suffix = r"(?:字段|field|输入框|控件|下拉|列|区域|section|textarea|select)"
    for context in contexts:
        for quoted in re.findall(r"[`\"'「『]([^`\"'」』]{1,80})[`\"'」』]", context):
            add(quoted)
        for match in re.finditer(
            rf"([A-Za-z][A-Za-z0-9_ /&().-]{{1,80}}?)\s*{label_suffix}",
            context,
            flags=re.I,
        ):
            add(match.group(1))

    return out


def _field_label_candidates(field: str, read_spec: str = "") -> list[str]:
    out = [field]
    for candidate in read_spec_label_candidates(read_spec, field):
        if candidate not in out:
            out.append(candidate)
    return out


def read_form_control_returns(
    form_controls: list[dict] | None,
    returns: list[str],
    read_spec: str = "",
) -> dict[str, str]:
    """Read declared return fields from the platform-neutral ``obs.form_controls`` (DOM-captured
    select/input values), BEFORE any vision guess. A native ``<select>``'s selected option text is
    authoritative — for a multiselect the primary value is the FIRST selected option (live 185:
    Material), never the numeric option id and never the first *listed* option (the WS08 vision
    misread that returned Burlap instead of the selected Cotton).

    Returns native-select fields once a matching control is found, even when the selected value is
    empty: an unselected ``<select multiple>`` is an authoritative empty value, not a cue to guess
    from the option list. Other empty controls are omitted so the caller can still try vision for
    fields that were not resolved. Empty/None form_controls (iPhone/Android) → {} → pure vision, as
    before. Matching is exact-normalized label, else a substring containment either way."""
    if not form_controls:
        return {}
    out: dict[str, str] = {}
    for field in returns:
        candidates = [(raw, _norm_label(raw)) for raw in _field_label_candidates(field, read_spec)]
        candidates = [(raw, norm) for raw, norm in candidates if norm]
        if not candidates:
            continue
        best: dict | None = None
        for _, nf in candidates:
            for fc in form_controls:
                nl = _norm_label(fc.get("label") or fc.get("name") or fc.get("id") or "")
                if not nl:
                    continue
                if nl == nf or nf in nl or nl in nf:
                    best = fc
                    if nl == nf:
                        break
            if best is not None:
                if _norm_label(best.get("label") or best.get("name") or best.get("id") or "") == nf:
                    break
        if best is None:
            continue
        if (best.get("kind") or "") == "native_select":
            selected = best.get("selected_text") or ""
            value = selected.split(",")[0].strip() if selected else ""
            out[field] = value
        else:
            value = str(best.get("selected_text") or best.get("value") or "")
        if (best.get("kind") or "") != "native_select" and str(value).strip():
            out[field] = value
    return out


class _FieldRead(BaseModel):
    field: str = Field(description="字段名（照抄请求里的字段）")
    evidence: str = Field(default="", description="界面上与该字段相关的信号（文字/图标/颜色/位置），先写这个")
    value: str = Field(default="", description="据 evidence + 判读提示得出的字段文字值；图标/颜色也要判成文字，确实没有才留空")


class _StructuredRead(BaseModel):
    reads: list[_FieldRead] = Field(default_factory=list)


def structured_read(
    png_bytes: bytes,
    returns: list[str],
    read_spec: str = "",
    check_knowledge: str = "",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
) -> dict[str, str]:
    """Read `returns` fields off the frame -> {field: value} (empty when not readable).

    `read_spec` is the task-level read instruction the decomposer generated from the user goal
    (what each field means + how to judge it off the UI) — the PRIMARY judgment guidance, so the
    extraction semantics live in the program, not in this hardcoded prompt. `check_knowledge`
    (the app's _check.md signal conventions) is a supplementary reference for icon/colour cues.
    `prepare_vision_prompt_png` is supplied by the platform bundle; shared code must not assume
    iPhone Retina geometry for browser/android observations."""
    if not returns:
        return {}
    cfg = resolve_llm_config("reader")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    text = render_prompt_context([
        ContextBlock(
            id="runtime.read.requested_fields",
            source_type="runtime_state",
            source="orchestrator.read",
            ttl="turn",
            priority=20,
            content=f"读取以下字段的当前值：{'、'.join(returns)}。",
        ),
        ContextBlock(
            id="runtime.read.spec",
            source_type="runtime_state",
            source="program.read_spec",
            ttl="task",
            priority=25,
            content="【读取说明】（任务定义，按此判读每个字段；优先于下方应用约定）：\n" + read_spec,
        ) if read_spec else None,
        ContextBlock(
            id="knowledge.check_rules",
            source_type="knowledge_base",
            source="knowledge_base",
            ttl="session",
            priority=50,
            content="【界面信号参考】（应用约定，某字段若以图标/颜色/位置表示可据此判读成文字值）：\n" + check_knowledge,
        ) if check_knowledge else None,
    ], label="structured_read.dynamic", report_sink=context_reports)
    prepare_png = prepare_vision_prompt_png or (lambda b: b)
    prepared = prepare_png(png_bytes)
    b64 = base64.b64encode(prepared).decode()
    messages = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    if context_reports is not None:
        image_text = f"[image_url omitted: image/png, {len(prepared)} bytes]"
        context_reports.append({
            "kind": "prompt_snapshot",
            "label": "structured_read.dynamic",
            "roles": [
                {
                    "role": "system",
                    "parts": [{
                        "label": "task_prompt",
                        "source_type": "prompt_asset",
                        "source": "task.orchestrator.structured_read",
                        "type": "text",
                        "text": _SYSTEM,
                        "chars": len(_SYSTEM),
                    }],
                },
                {
                    "role": "human",
                    "parts": [
                        {
                            "label": "structured_read.dynamic",
                            "source_type": "prompt_context",
                            "source": "render_prompt_context",
                            "type": "text",
                            "text": text,
                            "chars": len(text),
                        },
                        {
                            "label": "screenshot",
                            "source_type": "image",
                            "source": "observation",
                            "type": "image",
                            "text": image_text,
                            "chars": len(image_text),
                        },
                    ],
                },
            ],
        })
    result = invoke_structured(
        llm,
        messages,
        _StructuredRead,
        trace_sink=context_reports,
        trace_label="structured_read.dynamic",
    )
    # Keep only requested fields; default any missing to "" (当没有).
    by_field = {fr.field: (fr.value or "") for fr in result.reads}
    return {f: by_field.get(f, "") for f in returns}


class _RowRead(BaseModel):
    fields: list[_FieldRead] = Field(default_factory=list, description="该行各字段的读取")


class _RowsRead(BaseModel):
    rows: list[_RowRead] = Field(default_factory=list, description="逐行提取，每行一个对象")


def structured_read_rows(
    png_bytes: bytes,
    returns: list[str],
    read_spec: str = "",
    check_knowledge: str = "",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
) -> list[dict[str, str]]:
    """LIST read: extract EVERY matching row off the frame → [{field: value}, ...] (the runtime
    collection a `foreach` iterates). Same judgment scaffolding as structured_read; the difference is
    row-wise extraction (unknown row count, one object per row), returning the rows."""
    if not returns:
        return []
    cfg = resolve_llm_config("reader")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    text = render_prompt_context([
        ContextBlock(
            id="runtime.read.requested_fields",
            source_type="runtime_state",
            source="orchestrator.read",
            ttl="turn",
            priority=20,
            content=(
                f"【列表读取】把界面上所有匹配的行逐行提取，每行返回一个对象，对象的字段为：{'、'.join(returns)}。"
                "行数不定，把当前帧里所有目标行全部提取；某行某字段读不到就留空。"
            ),
        ),
        ContextBlock(
            id="runtime.read.spec",
            source_type="runtime_state",
            source="program.read_spec",
            ttl="task",
            priority=25,
            content="【读取说明】（任务定义，按此判读每个字段；优先于下方应用约定）：\n" + read_spec,
        ) if read_spec else None,
        ContextBlock(
            id="knowledge.check_rules",
            source_type="knowledge_base",
            source="knowledge_base",
            ttl="session",
            priority=50,
            content="【界面信号参考】（应用约定，某字段若以图标/颜色/位置表示可据此判读成文字值）：\n" + check_knowledge,
        ) if check_knowledge else None,
    ], label="structured_read_rows.dynamic", report_sink=context_reports)
    prepare_png = prepare_vision_prompt_png or (lambda b: b)
    prepared = prepare_png(png_bytes)
    b64 = base64.b64encode(prepared).decode()
    messages = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(content=[
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]),
    ]
    result = invoke_structured(
        llm, messages, _RowsRead,
        trace_sink=context_reports, trace_label="structured_read_rows.dynamic",
    )
    rows: list[dict[str, str]] = []
    for row in result.rows:
        by_field = {fr.field: (fr.value or "") for fr in row.fields}
        rows.append({f: by_field.get(f, "") for f in returns})
    return rows
