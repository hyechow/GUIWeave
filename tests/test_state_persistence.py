"""Task run-state persistence and statement report reduction (no statement_states)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gui_agent.core.run.content import ReadState, store_chunk_note
from gui_agent.core.run.result import make_result
from gui_agent.core.schemas import (
    PolicyContext,
    PolicyTurn,
    StatementOutcome,
    SupervisorStep,
)
from gui_agent.core.run.state import write_final_program_outcome
from gui_agent.reports.statement_reducer import StatementReportReducer


def _context(**extra) -> PolicyContext:
    data = {
        "goal": "goal",
        "supervisor_policy_name": "statement",
        "action_policy_name": "action",
    }
    data.update(extra)
    return PolicyContext.model_validate(data)


def test_policy_context_exposes_only_journal_and_program_outcome_state_domains():
    assert {"statements", "statement_states", "turns", "content_notes", "run"}.isdisjoint(
        PolicyContext.model_fields
    )
    assert {"journal", "outcome"}.issubset(PolicyContext.model_fields)


def test_content_notes_and_dedupe_are_rebuilt_only_from_journal_events():
    context = _context()
    step = SupervisorStep(should_act=False, summary="read", statement_id="s1")
    seen_rows: set[str] = set()
    assert store_chunk_note(
        "row one\nrow two",
        context,
        seen_rows,
        turn_no=1,
        sv_step=step,
    )
    raw = context.model_dump(mode="json")
    assert "content_notes" not in raw
    assert raw["journal"]["events"][0]["event_type"] == "content_note"

    restored = PolicyContext.model_validate(raw)
    rebuilt = ReadState._load_seen_rows(restored)
    assert store_chunk_note(
        "row one\nrow three",
        restored,
        rebuilt,
        turn_no=2,
        sv_step=step,
    )
    assert len(restored.journal.content_notes) == 2
    assert "row one" not in restored.journal.content_notes[-1]
    assert "row three" in restored.journal.content_notes[-1]


def test_outer_result_exposes_phase_and_verification_only():
    context = _context()
    result = make_result(
        context,
        "done",
        phase="completed",
        verification="confirmed",
    )

    assert result.phase == "completed"
    assert result.verification == "confirmed"
    assert {
        "goal_completed",
        "execution_completed",
        "goal_status",
        "stop_reason",
        "result_summary",
    }.isdisjoint(
        type(result).model_fields
    )
    assert result.summary == "done"
    assert result.output == "done"
    with pytest.raises(ValidationError, match="frozen"):
        result.phase = "failed"  # type: ignore[misc]


def test_agent_result_rejects_invalid_program_terminal_shape():
    from gui_agent.core.run.result import AgentResult

    with pytest.raises(ValidationError, match="requires verification"):
        AgentResult(goal="g", output="done", summary="done", phase="completed")


def test_write_final_program_outcome_patches_outcome_block(tmp_path: Path):
    path = tmp_path / "context.json"
    path.write_text(
        json.dumps({
            "goal": "g",
            "supervisor_policy_name": "m",
            "action_policy_name": "a",
            "journal": {"schema_version": 2, "events": []},
        }),
        encoding="utf-8",
    )
    write_final_program_outcome(
        path,
        {
            "phase": "completed",
            "verification": "confirmed",
            "summary": "ok",
        },
        output="done",
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["outcome"] == {
        "phase": "completed",
        "summary": "ok",
        "verification": "confirmed",
        "output": "done",
    }


def test_statement_reducer_folds_outcome_and_checklist():
    turns = [
        {
            "index": 1,
            "statement_instance_id": "i1:m1",
            "statement": {
                "id": "m1",
                "name": "打开页面",
                "description": "打开页面",
                "kind": "navigation",
                "success_condition": "页面已打开",
            },
            "supervisor": {
                "should_act": False,
                "summary": "完成",
                "statement_id": "m1",
                "statement_kind": "navigation",
                "outcome": {
                    "phase": "completed",
                    "summary": "完成",
                    "verification": "confirmed",
                    "reads": {"x": "1"},
                },
                "pre_existing": True,
            },
            "checker": {
                "status": "done",
                "reason": "已在目标页",
                "item_verdicts": [{"index": 1, "met": True, "evidence": "ok"}],
            },
            "operation_mode": "observation",
        }
    ]
    views = StatementReportReducer().reduce(events=turns)
    assert len(views) == 1
    view = views[0]
    assert view.statement_id == "m1"
    assert view.status == "done"
    assert view.reads == {"x": "1"}
    assert view.pre_existing is True
    assert view.checklist
    assert view.checklist[0]["status"] == "done"
