"""Content-note extraction and Journal deduplication."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from gui_agent.core.llm.reader import ContentReader, annotate_content_note, build_reader_instruction
from gui_agent.core.schemas import PolicyContext, SupervisorStep

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
    """Store a reader note after line-level dedupe."""

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
    context.journal.append_content(stored)
    return True


@dataclass
class ReadTurnResult:
    added_content: bool = False
    note_hash: str | None = None


class ReadState:
    """Own row-level dedupe for Transition-requested content reads."""

    def __init__(
        self,
        *,
        context: PolicyContext,
        reader: ContentReader,
    ) -> None:
        self.context = context
        self.reader = reader
        self.seen_rows = self._load_seen_rows(context)

    @staticmethod
    def _load_seen_rows(context: PolicyContext) -> set[str]:
        seen_rows: set[str] = set()
        for note in context.journal.content_notes:
            for line in note.splitlines():
                stripped = line.strip()
                if stripped:
                    seen_rows.add(note_hash(stripped))
        return seen_rows

    def process_turn(
        self,
        *,
        original_goal: str,
        sv_step: SupervisorStep,
        observation_png: bytes,
        bundle,
        turn_no: int,
        say,
    ) -> ReadTurnResult:
        """Process this turn's read instruction and return turn metadata."""
        del bundle
        result = ReadTurnResult()
        if not sv_step.read_instruction:
            return result

        reader_instruction = build_reader_instruction(original_goal, sv_step)
        say(f"读取内容: {reader_instruction}")
        note = self.reader.read(observation_png, reader_instruction)
        if store_chunk_note(
            note,
            self.context,
            self.seen_rows,
            turn_no=turn_no,
            sv_step=sv_step,
        ):
            result.added_content = True
            result.note_hash = note_hash(self.context.journal.content_notes[-1])
            say(f"内容摘要: {self.context.journal.content_notes[-1][:80]}...")
        else:
            say("内容摘要: 无新增/与已采集重复，未入库")
        return result
