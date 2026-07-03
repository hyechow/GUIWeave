"""callframe = milestone-as-function 调用约定的合同测试。

搬迁自 loop.py 的函数（missing/tighten/force_recovery/should_kickback）已由
test_orchestrator.py / test_feasibility_replan.py 覆盖；这里锁定新增的调用协议件：
ReturnRecoveryLedger（违约重试预算按调用点隔离）与 open_call（call 派发的 marshalling 口径）。
"""

from gui_agent.core.orchestrator.callframe import (
    MAX_EMPTY_RETURN_RECOVERIES,
    ReturnRecoveryLedger,
    open_call,
)
from gui_agent.core.orchestrator.program import Run


class _FakeSupervisor:
    def __init__(self):
        self.reseeds = []

    def reseed(self, milestone, task_type="action", fresh_advance=False):
        self.reseeds.append((milestone, task_type, fresh_advance))


def test_ledger_budget_exhausts_per_call_site():
    ledger = ReturnRecoveryLedger()
    run = Run(name="读订单状态", kind="action", var="r", returns=["状态"])
    attempts = [ledger.next_attempt(0, run) for _ in range(MAX_EMPTY_RETURN_RECOVERIES + 2)]
    assert attempts[:MAX_EMPTY_RETURN_RECOVERIES] == list(range(1, MAX_EMPTY_RETURN_RECOVERIES + 1))
    # 预算耗尽后必须持续返回 None（诚实失败，不是无限重试）
    assert attempts[MAX_EMPTY_RETURN_RECOVERIES:] == [None, None]


def test_ledger_isolates_call_sites():
    ledger = ReturnRecoveryLedger()
    run_a = Run(name="步骤A", kind="action", var="a", returns=["x"])
    run_b = Run(name="步骤B", kind="action", var="b", returns=["y"])
    assert ledger.next_attempt(0, run_a) == 1
    # 不同 run_index / 不同合同 = 不同调用点，预算互不侵蚀
    assert ledger.next_attempt(1, run_a) == 1
    assert ledger.next_attempt(0, run_b) == 1
    assert ledger.next_attempt(0, run_a) == 2


def test_ledger_key_follows_contract_not_name_suffix():
    """tighten 重写 name 但保留 var/returns —— 同一调用点的重试必须继续累计，不能因改名重置。"""
    ledger = ReturnRecoveryLedger()
    run = Run(name="读订单状态", kind="action", var="r", returns=["状态"])
    tightened = run.model_copy(update={"name": "读订单状态（继续定位返回字段：状态）"})
    assert ledger.next_attempt(3, run) == 1
    assert ledger.next_attempt(3, tightened) == 2


def test_open_call_marshals_run_and_reseeds():
    sup = _FakeSupervisor()
    ui_run = Run(name="点击保存", kind="action", success_condition="出现保存成功提示")
    milestone = open_call(sup, ui_run, 2, fresh_advance=True)
    assert len(sup.reseeds) == 1
    seeded, task_type, fresh = sup.reseeds[0]
    assert seeded is milestone
    assert seeded.name == "点击保存"
    assert task_type == "action"
    assert fresh is True


def test_open_call_read_run_gets_analysis_gate():
    """read/data_query 是查询：task_type 必须是 analysis，否则执行器的读取门不开。"""
    sup = _FakeSupervisor()
    read_run = Run(name="读取总数", kind="read", var="total", returns=["总数"])
    open_call(sup, read_run, 0)
    _, task_type, fresh = sup.reseeds[0]
    assert task_type == "analysis"
    assert fresh is False
