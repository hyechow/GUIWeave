from types import SimpleNamespace

from gui_agent.core.runner import _sync_milestone_states
from gui_agent.core.schemas import Milestone, PolicyContext, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
from scripts.report_builder import RunnerReportBuilder


def _context(**extra) -> PolicyContext:
    data = {
        "goal": "goal",
        "supervisor_policy_name": "milestone",
        "action_policy_name": "action",
    }
    data.update(extra)
    return PolicyContext.model_validate(data)


def test_policy_context_hydrates_legacy_milestone_runtime_state():
    ctx = _context(
        milestones=[
            {
                "id": "m1",
                "name": "打开页面",
                "description": "打开页面",
                "kind": "navigation",
                "success_condition": "页面已打开",
                "status": "done",
                "retry_count": 1,
                "done_check": {"status": "done", "reason": "已在目标页"},
            }
        ],
    )

    state = ctx.milestone_states["m1"]
    assert state.status == "done"
    assert state.retry_count == 1
    assert state.done_check["reason"] == "已在目标页"
    assert ctx.milestones[0]["done_check"]["status"] == "done"


def test_runner_syncs_structured_and_legacy_milestone_state():
    milestone = Milestone(
        id="m1",
        name="打开页面",
        description="打开页面",
        kind="navigation",
        success_condition="页面已打开",
    )
    milestone.status = "done"
    milestone.retry_count = 2
    check = _SingleCheckResult(
        status="done",
        reason="已在目标页",
        summary="目标页",
    )
    supervisor = SimpleNamespace(
        _milestones={"m1": milestone},
        _milestone_done_checks={"m1": check},
        _last_page_identity={"m1": "目标页"},
        _scroll_counts={"m1": 3},
        _progress_values={"m1": ["1", "2"]},
    )
    turn = PolicyTurn(
        index=1,
        observation_source="screen",
        supervisor=SupervisorStep(
            should_act=False,
            stop=True,
            stop_reason="所有子目标已完成",
            goal_completed=True,
            summary="完成",
            milestone_id="m1",
            milestone_kind="navigation",
            completion_strategy="visible_once",
            pre_existing=True,
        ),
        read_note_hash="abc",
    )
    ctx = _context(
        milestones=[
            {
                "id": "m1",
                "name": "打开页面",
                "description": "打开页面",
                "kind": "navigation",
                "success_condition": "页面已打开",
            }
        ],
        turns=[turn],
    )

    _sync_milestone_states(supervisor, ctx)

    state = ctx.milestone_states["m1"]
    assert state.status == "done"
    assert state.retry_count == 2
    assert state.done_check["reason"] == "已在目标页"
    assert state.last_page_identity == "目标页"
    assert state.scroll_count == 3
    assert state.progress_values == ["1", "2"]
    assert state.note_hashes == ["abc"]
    assert state.pre_existing is True
    assert ctx.milestones[0]["done_check"]["reason"] == "已在目标页"


def test_report_builder_reads_done_check_from_milestone_states(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "context.json").write_text(
        """
{
  "goal": "goal",
  "supervisor_policy_name": "milestone",
  "action_policy_name": "action",
  "milestones": [
    {
      "id": "m1",
      "name": "打开页面",
      "description": "打开页面",
      "kind": "navigation",
      "success_condition": "页面已打开"
    }
  ],
  "milestone_states": {
    "m1": {
      "id": "m1",
      "status": "done",
      "done_check": {"status": "done", "reason": "已在目标页"}
    }
  },
  "turns": [
    {
      "index": 1,
      "timestamp": "2026-06-16T12:00:00",
      "observation_source": "screen",
      "supervisor": {
        "should_act": false,
        "instruction": null,
        "stop": true,
        "stop_reason": "所有子目标已完成",
        "goal_completed": true,
        "summary": "完成",
        "milestone_id": "m1",
        "milestone_kind": "navigation",
        "completion_strategy": "visible_once"
      },
      "action_decision": null,
      "executed": false
    }
  ]
}
""",
        encoding="utf-8",
    )

    data = RunnerReportBuilder().build(run_dir)

    assert data.pages[0].verify_checker["reason"] == "已在目标页"
    assert data.milestones[0]["status"] == "done"
