"""StatementReportReducer projects recorded invocations without cross-talk."""
from gui_agent.reports.statement_reducer import StatementReportReducer


def _turn(
    index,
    *,
    instance_id,
    statement_id,
    outcome_phase=None,
    outputs=None,
    summary="s",
    transition=None,
):
    statement = {
        "id": statement_id,
        "executor": "interact",
        "goal": f"stmt {statement_id}",
        "success": f"done {statement_id}",
    }
    if outcome_phase:
        return {
            "event_type": "statement_outcome",
            "after_turn": max(0, index - 1),
            "statement_instance_id": instance_id,
            "statement_id": statement_id,
            "statement": statement,
            "outcome": {
                "phase": outcome_phase,
                "summary": summary,
                "outputs": outputs or {},
            },
            **({"transition": transition} if transition else {}),
        }
    return {
        "event_type": "turn",
        "index": index,
        "statement_instance_id": instance_id,
        "statement": statement,
        "supervisor": {
            "statement_id": statement_id, "summary": summary,
        },
        **({"transition": transition} if transition else {}),
    }


def test_two_invocations_of_same_statement_get_distinct_views():
    turns = [
        _turn(1, instance_id="i1:s1", statement_id="s1", outcome_phase="completed"),
        _turn(2, instance_id="i2:s1", statement_id="s1", outcome_phase="completed"),
    ]
    views = StatementReportReducer().reduce(events=turns)
    assert len(views) == 2
    assert {v.instance_id for v in views} == {"i1:s1", "i2:s1"}
    # Same stable statement_id, different invocation buckets — no cross-talk.
    assert {v.statement_id for v in views} == {"s1"}


def test_terminal_outputs_stay_isolated_by_instance_id():
    turns = [
        _turn(1, instance_id="i1:s1", statement_id="s1",
              outcome_phase="completed", outputs={"rating": 5}),
        _turn(2, instance_id="i2:s1", statement_id="s1",
              outcome_phase="completed", outputs={"rating": 3}),
    ]
    views = StatementReportReducer().reduce(events=turns)
    by_inst = {v.instance_id: v for v in views}
    assert by_inst["i1:s1"].outputs == {"rating": 5}
    assert by_inst["i2:s1"].outputs == {"rating": 3}


def test_transition_completion_projects_report_check_without_mutable_checker_state():
    turn = _turn(
        1,
        instance_id="i1:s1",
        statement_id="s1",
        transition={
            "proposal": {
                "kind": "complete",
                "reason": "目标状态已出现",
                "summary": "完成",
                "effect_status": "confirmed",
                "evidence": [
                    {"source": "current_observation", "claim": "成功提示可见"}
                ],
            },
            "validation_error": "",
        },
    )

    (view,) = StatementReportReducer().reduce(events=[turn])

    assert view.acceptance["status"] == "done"
    assert view.acceptance["visible_evidence"] == ["成功提示可见"]
    assert view.checklist and view.checklist[0]["status"] == "done"


def test_structural_completion_projects_terminal_outcome_without_transition():
    turn = _turn(
        1,
        instance_id="i1:s1",
        statement_id="s1",
        outcome_phase="completed",
        summary="权威结构化状态满足合同",
    )

    (view,) = StatementReportReducer().reduce(events=[turn])

    assert view.status == "done"
    assert view.acceptance["status"] == "done"
    assert "权威结构化状态" in view.acceptance["reason"]


def test_transition_action_does_not_create_report_retry_state():
    turn = _turn(
        1,
        instance_id="i1:s1",
        statement_id="s1",
        transition={
            "proposal": {
                    "kind": "act",
                "reason": "当前入口无效，选择另一个可见入口",
                "evidence": [],
            },
            "validation_error": "",
        },
    )

    (view,) = StatementReportReducer().reduce(events=[turn])

    assert not hasattr(view, "retry_count")
    assert view.status == "running"
