"""StatementReportReducer projects recorded invocations without cross-talk."""
from gui_agent.reports.statement_reducer import StatementReportReducer


def _turn(index, *, instance_id, statement_id, outcome_phase=None, reads=None, summary="s"):
    return {
        "index": index,
        "statement_instance_id": instance_id,
        "statement": {"id": statement_id, "name": f"stmt {statement_id}",
                       "kind": "action", "success_condition": f"done {statement_id}"},
        "supervisor": {
            "statement_id": statement_id, "summary": summary,
            **({"outcome": {"phase": outcome_phase, "reads": reads or {}}} if outcome_phase else {}),
        },
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


def test_terminal_turn_reads_stay_isolated_by_instance_id():
    turns = [
        _turn(1, instance_id="i1:s1", statement_id="s1",
              outcome_phase="completed", reads={"rating": "5"}),
        _turn(2, instance_id="i2:s1", statement_id="s1",
              outcome_phase="completed", reads={"rating": "3"}),
    ]
    views = StatementReportReducer().reduce(events=turns)
    by_inst = {v.instance_id: v for v in views}
    assert by_inst["i1:s1"].reads == {"rating": "5"}
    assert by_inst["i2:s1"].reads == {"rating": "3"}  # not overwritten by the other invocation
