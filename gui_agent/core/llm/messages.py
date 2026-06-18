"""Shared LLM message assembly for vision prompts."""

from __future__ import annotations

import base64
from collections.abc import Callable, MutableSequence, Sequence

from langchain_core.messages import HumanMessage, SystemMessage

from gui_agent.context.blocks import ContextBlock, ContextBudgeter, render_context_blocks
from gui_agent.context.runtime import DEFAULT_CONTEXT_BLOCKS_MAX_CHARS, current_date_block
from gui_agent.core.policies.base import resize_to_logical_png
from gui_agent.core.schemas import Observation


def prepare_prompt_png(
    png_bytes: bytes,
    *,
    image_resize: str = "retina",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
) -> bytes:
    """Apply the platform vision-image hook before sending a screenshot to the model."""
    if prepare_vision_prompt_png is not None:
        return prepare_vision_prompt_png(png_bytes)
    if image_resize == "none":
        return png_bytes
    return resize_to_logical_png(png_bytes)


def assemble_messages(
    task_prompt: str,
    observation: Observation | bytes | None,
    *,
    system_blocks: Sequence[ContextBlock | None] = (),
    human_blocks: Sequence[ContextBlock | None] = (),
    image_resize: str = "retina",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    max_chars: int | None = None,
    label: str = "prompt",
    context_reports: MutableSequence[dict] | Callable[[dict], None] | None = None,
    decision_text: str = "请根据当前屏幕做出决策。",
) -> list:
    """Assemble a vision LLM call through one budgeted context path.

    ``task_prompt`` is the stable task instruction. Runtime data belongs in
    ``system_blocks`` or ``human_blocks`` and is budgeted in one union pass before
    being placed in the message.
    """
    sys_live = [b for b in system_blocks if b is not None and (b.content or "").strip()]
    hum_live = [b for b in human_blocks if b is not None and (b.content or "").strip()]
    budgeter = ContextBudgeter(max_chars or DEFAULT_CONTEXT_BLOCKS_MAX_CHARS)
    result = budgeter.apply([*sys_live, *hum_live])
    if context_reports is not None and result.decisions:
        _append_report(context_reports, result.to_report(label=label))
    if result.dropped:
        names = "、".join(f"{b.id}[{b.budget}]({len(b.render())}字)" for b in result.dropped)
        print(f"  [ContextBudget] {label} 超预算({budgeter.max_chars}字),丢弃 {len(result.dropped)} 块: {names}")
    if result.over_budget:
        print(f"  [ContextBudget] ⚠️ {label} 必留块已达 {result.kept_chars} 字 / 上限 {budgeter.max_chars} 字")

    kept = {id(b) for b in result.kept}
    # Provenance ([context: id | type=… | source=… | ttl=…]) is for the trace/report only —
    # to_report(...) above carries it. The MODEL sees just the block bodies (each already
    # carries its own "## 标题" markdown header), so render headerless: no machine-tag token
    # tax / salience dilution in the prompt.
    sys_text = render_context_blocks([b for b in sys_live if id(b) in kept], include_headers=False)
    hum_text = render_context_blocks([b for b in hum_live if id(b) in kept], include_headers=False)

    date_block = current_date_block()
    system = (
        task_prompt
        + (f"\n\n{sys_text}" if sys_text else "")
        + f"\n\n{date_block.render(include_header=False)}"
    )
    human_content: list = []
    if hum_text:
        human_content.append({"type": "text", "text": f"{hum_text}\n\n"})
    if decision_text:
        human_content.append({"type": "text", "text": decision_text})

    png_bytes = _png_bytes(observation)
    image_report: dict | None = None
    if png_bytes:
        prepared = prepare_prompt_png(
            png_bytes,
            image_resize=image_resize,
            prepare_vision_prompt_png=prepare_vision_prompt_png,
        )
        image_report = {
            "type": "image",
            "label": "screenshot",
            "source": "observation",
            "bytes": len(prepared),
            "text": f"[image_url omitted: image/png, {len(prepared)} bytes, image_resize={image_resize}]",
        }
        b64 = base64.b64encode(prepared).decode()
        human_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    if context_reports is not None:
        _append_report(
            context_reports,
            _prompt_snapshot_report(
                label=label,
                task_prompt=task_prompt,
                system_blocks=[b for b in sys_live if id(b) in kept],
                human_blocks=[b for b in hum_live if id(b) in kept],
                date_block=date_block,
                decision_text=decision_text,
                image_report=image_report,
            ),
        )

    return [SystemMessage(content=system), HumanMessage(content=human_content)]


def _png_bytes(observation: Observation | bytes | None) -> bytes | None:
    if observation is None:
        return None
    if isinstance(observation, bytes):
        return observation
    return observation.png_bytes


def _append_report(
    sink: MutableSequence[dict] | Callable[[dict], None],
    report: dict,
) -> None:
    if callable(sink):
        sink(report)
    else:
        sink.append(report)


def _prompt_snapshot_report(
    *,
    label: str,
    task_prompt: str,
    system_blocks: Sequence[ContextBlock],
    human_blocks: Sequence[ContextBlock],
    date_block: ContextBlock,
    decision_text: str,
    image_report: dict | None,
) -> dict:
    """Model-visible prompt snapshot for report debugging.

    This records text parts in assembly order, but deliberately omits base64 image data.
    The returned structure is diagnostic-only; it does not affect the messages sent to the model.
    """
    system_parts = [
        {
            "label": "task_prompt",
            "source_type": "prompt_asset",
            "source": "task_prompt",
            "type": "text",
            "text": task_prompt,
            "chars": len(task_prompt),
        },
        *[_context_part(block) for block in system_blocks],
        _context_part(date_block),
    ]
    human_parts = [_context_part(block) for block in human_blocks]
    if decision_text:
        human_parts.append({
            "label": "decision_text",
            "source_type": "runtime_state",
            "source": "assemble_messages",
            "type": "text",
            "text": decision_text,
            "chars": len(decision_text),
        })
    if image_report is not None:
        human_parts.append({
            **image_report,
            "chars": len(str(image_report.get("text") or "")),
        })
    return {
        "kind": "prompt_snapshot",
        "label": label,
        "roles": [
            {"role": "system", "parts": system_parts},
            {"role": "human", "parts": human_parts},
        ],
    }


def _context_part(block: ContextBlock) -> dict:
    text = block.render(include_header=False)
    return {
        "label": block.id,
        "source_type": block.source_type,
        "source": block.source,
        "ttl": block.ttl,
        "budget": block.budget,
        "type": "text",
        "text": text,
        "chars": len(text),
    }
