"""恢复账本(异常体系 Stage A)契约测试。

RecoveryLedger = 继承的空返回预算(原 callframe ReturnRecoveryLedger,行为必须逐字节不变)
+ append-only 事件账。callframe 的 re-export 面必须保持稳定(既有进口方不换行)。
"""

from types import SimpleNamespace

from gui_agent.core.orchestrator import callframe
from gui_agent.core.orchestrator.recovery import (
    MAX_EMPTY_RETURN_RECOVERIES,
    RecoveryEvent,
    RecoveryLedger,
    ReturnRecoveryLedger,
)


def _run(var="m1", name="读取统计", returns=("total",)):
    return SimpleNamespace(var=var, name=name, returns=list(returns))


# ── inherited empty-return budget: behavior identical to the old callframe ledger ──


def test_return_budget_consumes_then_exhausts():
    ledger = RecoveryLedger()
    run = _run()
    attempts = [ledger.next_attempt(0, run) for _ in range(MAX_EMPTY_RETURN_RECOVERIES + 2)]
    assert attempts[:MAX_EMPTY_RETURN_RECOVERIES] == list(range(1, MAX_EMPTY_RETURN_RECOVERIES + 1))
    assert attempts[MAX_EMPTY_RETURN_RECOVERIES:] == [None, None]


def test_return_budget_isolated_per_call_site():
    ledger = RecoveryLedger()
    a, b = _run(var="a"), _run(var="b")
    for _ in range(MAX_EMPTY_RETURN_RECOVERIES):
        assert ledger.next_attempt(0, a) is not None
    assert ledger.next_attempt(0, a) is None      # a exhausted
    assert ledger.next_attempt(0, b) == 1          # b untouched
    assert ledger.next_attempt(1, a) == 1          # same run, different index = different site


def test_callframe_reexport_is_the_same_class():
    # The ABI surface stays put: existing `from callframe import ReturnRecoveryLedger` importers
    # get the moved class, and a RecoveryLedger satisfies the old budget contract.
    assert callframe.ReturnRecoveryLedger is ReturnRecoveryLedger
    assert callframe.MAX_EMPTY_RETURN_RECOVERIES == MAX_EMPTY_RETURN_RECOVERIES
    assert isinstance(RecoveryLedger(), callframe.ReturnRecoveryLedger)


# ── event log ────────────────────────────────────────────────────────────────────────


def test_record_appends_and_returns_event():
    ledger = RecoveryLedger()
    e = ledger.record("contract_violation", "tighten_return", "m1",
                      detail="缺 total", outcome="tighten 1/3")
    assert isinstance(e, RecoveryEvent)
    assert ledger.events == [e]
    assert e.cls == "contract_violation" and e.site == "m1"


def test_summary_counts_by_class_and_mechanism():
    ledger = RecoveryLedger()
    ledger.record("contract_violation", "tighten_return", "m1", outcome="tighten 1/3")
    ledger.record("contract_violation", "tighten_return", "m1", outcome="tighten 2/3")
    ledger.record("infeasible_route", "kickback_redecompose", "m2", outcome="replanned")
    ledger.record("data_source_error", "sql_repair", "q1", outcome="recovered")
    s = ledger.summary()
    assert s["total"] == 4
    assert s["by_class"] == {"contract_violation": 2, "infeasible_route": 1, "data_source_error": 1}
    assert s["by_mechanism"]["tighten_return"] == 2
    assert [e["outcome"] for e in s["events"]][:2] == ["tighten 1/3", "tighten 2/3"]


def test_summary_is_json_serializable():
    import json

    ledger = RecoveryLedger()
    ledger.record("data_source_error", "data_query_failure", "q2",
                  detail="数据源为空", outcome="replan_candidate")
    json.dumps(ledger.summary(), ensure_ascii=False)  # must not raise


def test_budget_and_events_are_independent():
    # Consuming the inherited budget must not fabricate events; the caller records explicitly
    # (Stage A: record-only ledger, budgets keep living with their mechanisms).
    ledger = RecoveryLedger()
    assert ledger.next_attempt(0, _run()) == 1
    assert ledger.events == []
