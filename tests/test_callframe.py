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


def test_enum_domain_rejects_out_of_domain_value():
    """185 类 bug 的合同化：Material 读到列表首项而非已选项 → 出域拒收，不静默给错答。"""
    from gui_agent.core.orchestrator.callframe import check_return_contract

    run = Run(
        name="读取创建结果", kind="action", var="c",
        returns=["创建结果"],
        return_domains={"创建结果": "enum:成功|失败"},
    )
    report = check_return_contract(run, {"创建结果": "页面仍在加载中"})
    assert bool(report) and report.violated_fields == ["创建结果"]
    assert "枚举域" in report.out_of_domain[0].reason
    # 域内值（大小写不敏感）通过
    assert not check_return_contract(run, {"创建结果": "成功"})


def test_inferred_url_and_number_domains():
    from gui_agent.core.orchestrator.callframe import check_return_contract

    run = Run(
        name="读取行详情", kind="navigation", var="d",
        returns=["详情URL", "记录总数", "备注"],
    )
    # URL 字段读到散文、计数字段读到无数字文本 → 双违约；备注无域线索不约束
    report = check_return_contract(
        run, {"详情URL": "页面上没有链接", "记录总数": "很多", "备注": "随便什么"}
    )
    assert {v.field for v in report.out_of_domain} == {"详情URL", "记录总数"}
    # 相对路径 URL、含数字计数 → 合同满足
    ok = check_return_contract(
        run, {"详情URL": "customer/index/edit/id/5/", "记录总数": "153 records", "备注": "ok"}
    )
    assert not ok


def test_empty_values_are_missing_not_domain_violations():
    from gui_agent.core.orchestrator.callframe import check_return_contract

    run = Run(name="读取", kind="action", var="r", returns=["详情URL"])
    report = check_return_contract(run, {"详情URL": ""})
    assert report.missing == ["详情URL"] and report.out_of_domain == []


def test_domain_check_exempts_pure_reads():
    from gui_agent.core.orchestrator.callframe import out_of_domain_return_fields

    read_run = Run(name="读", kind="read", var="r", returns=["详情URL"],
                   return_domains={"详情URL": "url"})
    assert out_of_domain_return_fields(read_run, {"详情URL": "不是链接"}) == []


def test_tighten_carries_domain_violation_context():
    from gui_agent.core.orchestrator.callframe import (
        check_return_contract,
        tighten_ui_return_run,
    )

    run = Run(
        name="触发检测", kind="action", var="p",
        returns=["是否可达"], success_condition="已触发",
        return_domains={"是否可达": "enum:可达|不可达"},
    )
    report = check_return_contract(run, {"是否可达": "检测按钮已高亮"})
    tightened = tighten_ui_return_run(
        run, report.missing, {"是否可达": "检测按钮已高亮"},
        attempt=1, violations=report.out_of_domain,
    )
    assert "是否可达" in tightened.name
    assert "检测按钮已高亮" in tightened.success_condition  # 上次的坏值被点名
    assert "不要再读同一处" in tightened.success_condition


def test_decomposer_draft_maps_return_domains_only_for_declared_fields():
    from gui_agent.core.orchestrator.decomposer import _StepDraft, _to_stmts

    draft = _StepDraft(
        op="run", run_kind="action", var="c", name="创建", returns=["创建结果"],
        return_domains={"创建结果": "enum:成功|失败", "幽灵字段": "number"},
    )
    (run,) = _to_stmts([draft])
    assert run.return_domains == {"创建结果": "enum:成功|失败"}


def test_open_call_read_run_gets_analysis_gate():
    """read/data_query 是查询：task_type 必须是 analysis，否则执行器的读取门不开。"""
    sup = _FakeSupervisor()
    read_run = Run(name="读取总数", kind="read", var="total", returns=["总数"])
    open_call(sup, read_run, 0)
    _, task_type, fresh = sup.reseeds[0]
    assert task_type == "analysis"
    assert fresh is False
