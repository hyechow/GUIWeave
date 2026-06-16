import json
from types import SimpleNamespace

from gui_agent.core.schemas import Milestone, PolicyContext, PolicyTurn, SupervisorStep
from gui_agent.core.state import sync_milestone_states, write_final_run_state
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
from scripts.report_builder import RunnerReportBuilder, generate_html


def _context(**extra) -> PolicyContext:
    data = {
        "goal": "goal",
        "supervisor_policy_name": "milestone",
        "action_policy_name": "action",
    }
    data.update(extra)
    return PolicyContext.model_validate(data)


def test_policy_context_strips_runtime_fields_from_static_milestones():
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

    assert "done_check" not in ctx.milestones[0]
    assert "status" not in ctx.milestones[0]
    assert "retry_count" not in ctx.milestones[0]


def test_runner_syncs_milestone_state_from_supervisor_snapshot():
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
    supervisor = SimpleNamespace(runtime_state_snapshot=lambda: {
        "milestones": {"m1": {"status": milestone.status, "retry_count": milestone.retry_count}},
        "done_checks": {"m1": check.model_dump(mode="json", exclude_none=True)},
        "last_page_identity": {"m1": "目标页"},
        "scroll_counts": {"m1": 3},
        "progress_values": {"m1": ["1", "2"]},
    })
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

    sync_milestone_states(supervisor, ctx)

    state = ctx.milestone_states["m1"]
    assert state.status == "done"
    assert state.retry_count == 2
    assert state.done_check["reason"] == "已在目标页"
    assert state.last_page_identity == "目标页"
    assert state.scroll_count == 3
    assert state.progress_values == ["1", "2"]
    assert state.note_hashes == ["abc"]
    assert state.pre_existing is True
    assert state.checklist[0].status == "done"
    assert state.checklist[0].text == "页面已打开"
    assert "done_check" not in ctx.milestones[0]
    assert "checklist" not in ctx.milestones[0]


def test_runner_updates_checklist_from_in_progress_checker():
    ctx = _context(
        milestones=[
            {
                "id": "m1",
                "name": "填写表单",
                "description": "填写表单",
                "kind": "action",
                "success_condition": "名称和站点都已保存",
            }
        ],
        turns=[
            PolicyTurn(
                index=1,
                observation_source="screen",
                supervisor=SupervisorStep(
                    should_act=True,
                    instruction="填写名称",
                    stop=False,
                    goal_completed=False,
                    summary="名称已填，站点未选",
                    milestone_id="m1",
                    milestone_kind="action",
                    completion_strategy="visible_once",
                ),
                checker={
                    "status": "in_progress",
                    "reason": "名称已显示，但站点仍为空",
                    "summary": "表单页",
                    "visible_evidence": ["名称 lucas 已显示"],
                    "missing_evidence": ["站点未选择 s10"],
                },
            )
        ],
    )

    sync_milestone_states(SimpleNamespace(runtime_state_snapshot=lambda: {}), ctx)

    items = {item.text: item for item in ctx.milestone_states["m1"].checklist}
    assert items["名称和站点都已保存"].status == "pending"
    assert items["名称和站点都已保存"].evidence == ["名称 lucas 已显示"]
    assert items["站点未选择 s10"].status == "pending"


def test_write_final_run_state_writes_only_structured_run(tmp_path):
    path = tmp_path / "context.json"
    path.write_text(
        json.dumps(
            {
                "goal": "goal",
                "supervisor_policy_name": "milestone",
                "action_policy_name": "action",
                "turns": [],
                "output": "old",
                "stop_reason": "old",
                "run_status": "stopped",
                "goal_completed": False,
            }
        ),
        encoding="utf-8",
    )

    write_final_run_state(
        path,
        {"stop_reason": "用户按 ESC 中止 agent-loop", "goal_completed": False},
        "已中止",
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run"] == {
        "status": "interrupted",
        "stop_reason": "用户按 ESC 中止 agent-loop",
        "goal_completed": False,
        "output": "已中止",
    }
    assert "output" not in data
    assert "stop_reason" not in data
    assert "run_status" not in data
    assert "goal_completed" not in data


def test_milestone_supervisor_exposes_runtime_state_snapshot():
    milestone = Milestone(
        id="m1",
        name="打开页面",
        description="打开页面",
        kind="navigation",
        success_condition="页面已打开",
    )
    milestone.status = "done"
    milestone.retry_count = 1
    check = _SingleCheckResult(
        status="done",
        reason="已在目标页",
        summary="目标页",
    )
    policy = MilestoneSupervisorPolicy()
    policy._milestones = {"m1": milestone}
    policy._milestone_done_checks = {"m1": check}
    policy._last_page_identity = {"m1": "目标页"}
    policy._scroll_counts = {"m1": 2}
    policy._progress_values = {"m1": ["1", "2"]}

    snapshot = policy.runtime_state_snapshot()

    assert snapshot["milestones"]["m1"]["status"] == "done"
    assert snapshot["milestones"]["m1"]["retry_count"] == 1
    assert snapshot["done_checks"]["m1"]["reason"] == "已在目标页"
    assert snapshot["last_page_identity"]["m1"] == "目标页"
    assert snapshot["scroll_counts"]["m1"] == 2
    assert snapshot["progress_values"]["m1"] == ["1", "2"]


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
      "done_check": {"status": "done", "reason": "已在目标页"},
      "checklist": [
        {
          "id": "accept-1",
          "text": "页面已打开",
          "status": "done",
          "evidence": ["已在目标页"],
          "source": "checker:success_condition"
        }
      ]
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
    assert data.pages[0].checklist[0]["status"] == "done"
    html = generate_html(data)
    assert "milestone-checklist" in html
    assert "页面已打开" in html
