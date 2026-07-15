"""Task run-state persistence and statement report reduction (no milestone_states)."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import (
    PolicyContext,
    PolicyTurn,
    StatementOutcome,
    SupervisorStep,
)
from gui_agent.core.run.state import write_final_run_state
from gui_agent.reports.statement_reducer import StatementReportReducer


def _context(**extra) -> PolicyContext:
    data = {
        "goal": "goal",
        "supervisor_policy_name": "milestone",
        "action_policy_name": "action",
    }
    data.update(extra)
    return PolicyContext.model_validate(data)


def test_policy_context_has_no_milestone_state_fields():
    assert "milestones" not in PolicyContext.model_fields
    assert "milestone_states" not in PolicyContext.model_fields


def test_write_final_run_state_patches_run_block(tmp_path: Path):
    path = tmp_path / "context.json"
    path.write_text(
        json.dumps({
            "goal": "g",
            "supervisor_policy_name": "m",
            "action_policy_name": "a",
            "turns": [],
            "run": {"status": "stopped"},
        }),
        encoding="utf-8",
    )
    write_final_run_state(
        path,
        {
            "execution_completed": True,
            "goal_completed": True,
            "goal_status": "confirmed",
            "stop_reason": "ok",
        },
        output="done",
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["run"]["status"] == "completed"
    assert raw["run"]["output"] == "done"
    assert raw["run"]["goal_completed"] is True


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
                "milestone_id": "m1",
                "milestone_kind": "navigation",
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
    views = StatementReportReducer().reduce(turns=turns, program={}, run_log=[])
    assert len(views) == 1
    view = views[0]
    assert view.statement_id == "m1"
    assert view.status == "done"
    assert view.reads == {"x": "1"}
    assert view.pre_existing is True
    assert view.checklist
    assert view.checklist[0]["status"] == "done"
