"""Runtime state for screenshot content reads."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from gui_agent.core.llm.reader import ContentReader, build_reader_instruction
from gui_agent.core.run.content import (
    STITCH_OVERLAP_PX,
    flush_and_read,
    note_hash,
    store_chunk_note,
)
from gui_agent.core.schemas import PolicyContext, SupervisorStep


@dataclass
class ReadTurnResult:
    added_content: bool = False
    note_hash: str | None = None


class ReadState:
    """Own pending reader futures, stitch buffers, and row-level dedupe state."""

    def __init__(
        self,
        *,
        context: PolicyContext,
        reader: ContentReader,
        pool: ThreadPoolExecutor,
    ) -> None:
        self.context = context
        self.reader = reader
        self.pool = pool
        self.stitch_acc: Any = None
        self.stitch_acc_mid: str | None = None
        self.stitch_acc_instr = ""
        self.stitch_acc_sv: SupervisorStep | None = None
        self.pending_read: tuple[Future, int, SupervisorStep] | None = None
        self.seen_rows = self._load_seen_rows(context)

    @staticmethod
    def _load_seen_rows(context: PolicyContext) -> set[str]:
        seen_rows: set[str] = set()
        for note in context.content_notes:
            for line in note.splitlines():
                stripped = line.strip()
                if stripped:
                    seen_rows.add(note_hash(stripped))
        return seen_rows

    def drain_pending(self, *, say) -> None:
        """Store any completed background reader notes on the main loop thread."""
        if self.pending_read is None:
            return
        fut, turn_no, sv_step = self.pending_read
        self.pending_read = None
        for note in fut.result():
            if store_chunk_note(
                note,
                self.context,
                self.seen_rows,
                turn_no=turn_no,
                sv_step=sv_step,
            ):
                say(f"内容摘要(块): {self.context.content_notes[-1][:80]}...")

    def flush(self, *, turn_no: int, say) -> None:
        """Flush the current stitched tail chunk, then clear stitch state."""
        flush_and_read(
            self.stitch_acc,
            self.stitch_acc_instr,
            self.stitch_acc_sv,
            self.reader,
            self.context,
            self.seen_rows,
            turn_no=turn_no,
            say=say,
        )
        self.stitch_acc = None
        self.stitch_acc_mid = None
        self.stitch_acc_instr = ""
        self.stitch_acc_sv = None

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
        """Drain prior reads, process this turn's read instruction, and return turn metadata."""
        result = ReadTurnResult()
        self.drain_pending(say=say)

        cur_mid = sv_step.milestone_id or "_global"
        if self.stitch_acc is not None and self.stitch_acc_mid != cur_mid:
            self.flush(turn_no=turn_no, say=say)

        if sv_step.read_instruction and not sv_step.allow_read:
            say(
                "跳过读取入库: 当前阶段不允许采集 "
                f"({sv_step.milestone_kind}/{sv_step.completion_strategy})"
            )
            return result
        if not sv_step.read_instruction:
            return result

        reader_instruction = build_reader_instruction(original_goal, sv_step)
        if sv_step.completion_strategy == "scroll_until_boundary":
            self._process_stitched_read(
                reader_instruction=reader_instruction,
                sv_step=sv_step,
                observation_png=observation_png,
                bundle=bundle,
                turn_no=turn_no,
                current_mid=cur_mid,
                result=result,
                say=say,
            )
            return result

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
            result.note_hash = note_hash(self.context.content_notes[-1])
            say(f"内容摘要: {self.context.content_notes[-1][:80]}...")
        else:
            say("内容摘要: 无新增/与已采集重复，未入库")
        return result

    def _process_stitched_read(
        self,
        *,
        reader_instruction: str,
        sv_step: SupervisorStep,
        observation_png: bytes,
        bundle,
        turn_no: int,
        current_mid: str,
        result: ReadTurnResult,
        say,
    ) -> None:
        if self.stitch_acc is None:
            self.stitch_acc = bundle.make_stitch_accumulator(overlap_px=STITCH_OVERLAP_PX)
            self.stitch_acc_mid = current_mid
        self.stitch_acc_instr = reader_instruction
        self.stitch_acc_sv = sv_step
        chunks, advanced = self.stitch_acc.feed(observation_png)
        result.added_content = advanced
        if chunks:
            say(
                f"读取内容: {reader_instruction}（{len(chunks)} 拼接块后台读, "
                f"待读 {self.stitch_acc.pending_px}px）"
            )
            self.pending_read = (
                self.pool.submit(
                    lambda cs=chunks, ins=reader_instruction: [
                        self.reader.read(chunk, ins) for chunk in cs
                    ]
                ),
                turn_no,
                sv_step,
            )
        elif advanced:
            say(f"拼接累积中（{self.stitch_acc.pending_px}px，未满一屏，暂不读）")
        else:
            say("列表未推进（滚动无效/到底），不追加")
