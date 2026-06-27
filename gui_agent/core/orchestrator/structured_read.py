"""Structured read: extract a read milestone's `returns` fields off the result frame.

The orchestrator's read primitive. A read milestone's job is single-frame data extraction
(the inspect insight): read the requested fields, interpreting non-text signals (icons /
colour / position) via the app's acceptance cues (_check.md), and return {field: value}
(empty when not readable = 当没有 — never blocks the program). The interpreter then branches
on those structured values, so "the checker saw it but the output didn't know" goes away.

This runs ON the milestone's done-frame (the supervisor advanced the read milestone there,
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
_PHONE_FIELD_RE = re.compile(r"phone|tel|telephone|mobile|电话|手机号|电话号码|号码", re.IGNORECASE)
_TEL_URI_RE = re.compile(r"tel:\+?([0-9][0-9\s().-]{6,}[0-9])", re.IGNORECASE)


class _FieldRead(BaseModel):
    field: str = Field(description="字段名（照抄请求里的字段）")
    evidence: str = Field(default="", description="界面上与该字段相关的信号（文字/图标/颜色/位置），先写这个")
    value: str = Field(default="", description="据 evidence + 判读提示得出的字段文字值；图标/颜色也要判成文字，确实没有才留空")


class _StructuredRead(BaseModel):
    reads: list[_FieldRead] = Field(default_factory=list)


def _phone_digits(text: str) -> str:
    digits = re.sub(r"\D+", "", text or "")
    return digits if len(digits) >= 7 else ""


def _normalize_phone_read_value(field: str, value: str, read_spec: str = "", text_source: str = "") -> str:
    """Canonicalize phone reads to digits, preferring explicit tel: accessibility links."""
    if not _PHONE_FIELD_RE.search(f"{field}\n{read_spec}"):
        return value
    for match in _TEL_URI_RE.finditer(text_source or ""):
        digits = _phone_digits(match.group(1))
        if digits:
            return digits
    digits = _phone_digits(value)
    return digits or value


def structured_read(
    png_bytes: bytes,
    returns: list[str],
    read_spec: str = "",
    check_knowledge: str = "",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
    text_source: str | None = None,
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
    if text_source is not None:
        # read_screen_text: feed the viewport a11y TEXT instead of the screenshot — the reader
        # extracts the same `returns` fields off text, not pixels (precise for numerals / lists /
        # in-browser API JSON that vision OCR misreads). text_source comes from the platform's
        # read_visible_text (ScreenTextReader), already viewport-filtered so read == seen.
        human_text = text + "\n\n【当前屏幕可见文本（a11y tree，视口内，读到的=看到的）】\n" + text_source
        messages = [
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=[{"type": "text", "text": human_text}]),
        ]
        obs_label, obs_source_type, obs_kind, obs_text = (
            "screen_text", "text", "text",
            f"[text_source: {len(text_source)} chars, viewport a11y text]",
        )
    else:
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
        obs_label, obs_source_type, obs_kind, obs_text = (
            "screenshot", "image", "image",
            f"[image_url omitted: image/png, {len(prepared)} bytes]",
        )
    if context_reports is not None:
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
                            "label": obs_label,
                            "source_type": obs_source_type,
                            "source": "observation",
                            "type": obs_kind,
                            "text": obs_text,
                            "chars": len(obs_text),
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
    return {
        f: _normalize_phone_read_value(f, by_field.get(f, ""), read_spec, text_source or "")
        for f in returns
    }


def resolve_text_source(run: object, platform: object) -> str | None:
    """If ``run`` opts into text-source read (``run.text_source``) AND the platform exposes
    ``read_visible_text`` (Android; ScreenTextReader capability), return the current frame's
    viewport a11y text; otherwise None (caller reads off the screenshot).

    Centralized so every structured_read call site (non_ui / non_interactive /
    loop._read_completed_run_returns) shares one probe + fallback. ``platform.client`` is the
    Device (loop.py: "device at platform.client"). Never raises: a text-path failure falls back
    to the screenshot read rather than blocking the run."""
    if not getattr(run, "text_source", False):
        return None
    device = getattr(platform, "client", None)
    if device is None or not hasattr(device, "read_visible_text"):
        return None
    try:
        return device.read_visible_text() or None
    except Exception:  # noqa: BLE001
        return None


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
