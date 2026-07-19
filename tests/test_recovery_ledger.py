"""Program recovery ledger records events without statement-private retry state."""

import json

from gui_agent.core.orchestrator.recovery import RecoveryEvent, RecoveryLedger


def test_record_appends_and_returns_event():
    ledger = RecoveryLedger()
    event = ledger.record(
        "infeasible_route",
        "kickback_redecompose",
        "s1",
        detail="current data source unavailable",
        outcome="replanned",
    )
    assert isinstance(event, RecoveryEvent)
    assert ledger.events == [event]


def test_summary_counts_by_class_and_mechanism():
    ledger = RecoveryLedger()
    ledger.record("infeasible_route", "kickback_redecompose", "s1")
    ledger.record("data_source_error", "data_plan_repair", "d1")
    summary = ledger.summary()
    assert summary["total"] == 2
    assert summary["by_class"] == {
        "infeasible_route": 1,
        "data_source_error": 1,
    }
    assert summary["by_mechanism"] == {
        "kickback_redecompose": 1,
        "data_plan_repair": 1,
    }
    json.dumps(summary, ensure_ascii=False)
