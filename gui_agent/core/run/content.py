"""Content-note deduplication and stitch flush helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from gui_agent.core.llm.reader import ContentReader, annotate_content_note
from gui_agent.core.schemas import PolicyContext

STITCH_OVERLAP_PX = 150  # chunk 间重叠像素：防止行被切断；重叠区像素相同 → 行级去重可靠


def ensure_note_hashes(context: PolicyContext) -> None:
    if context.content_notes and not context.content_note_hashes:
        context.content_note_hashes = [note_hash(note) for note in context.content_notes]


def note_hash(note: str) -> str:
    normalized = re.sub(r"\s+", "", note.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def store_chunk_note(
    note: str,
    context: PolicyContext,
    seen_rows: set[str],
    *,
    turn_no: int,
    sv_step,
) -> bool:
    """Store a stitched reader note after line-level dedupe."""

    if not note or note == "无相关内容":
        return False
    new_lines: list[str] = []
    for raw in note.splitlines():
        line = raw.strip()
        if not line:
            continue
        h = note_hash(line)
        if h in seen_rows:
            continue
        seen_rows.add(h)
        new_lines.append(line)
    if not new_lines:
        return False
    stored = annotate_content_note(
        "\n".join(new_lines),
        turn_no=turn_no,
        sv_step=sv_step,
        collection_scope=context.collection_scope,
    )
    context.content_notes.append(stored)
    return True


def flush_and_read(
    acc,
    instruction: str,
    sv_step,
    reader: ContentReader,
    context: PolicyContext,
    seen_rows: set[str],
    *,
    turn_no: int,
    say: Callable[[str], None],
) -> None:
    """Flush the tail chunk of a collection milestone and store reader output."""

    if acc is None or sv_step is None:
        return
    tail = acc.flush()
    if tail is None:
        return
    note = reader.read(tail, instruction or "")
    if store_chunk_note(note, context, seen_rows, turn_no=turn_no, sv_step=sv_step):
        say(f"内容摘要(收尾块): {context.content_notes[-1][:80]}...")
