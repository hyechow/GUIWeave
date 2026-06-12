"""Deterministic acceptance-shape guards in _validate_decomposition.

The decompose prompt states these shapes, but the model obeys them only ~60-70% of
the time (measured across repeated eval runs). The Guard runs as plain code in the
existing validate→feedback→retry loop, so the violation is caught and re-decomposed
rather than slipping through to a derailed run:

  - increment acceptance (「新增了 N 个」) → must be terminal-state
  - intermediate-state acceptance for action (「弹出弹窗」) → must be final result
  - record/report goal with no read step → must add a collection/read milestone
"""

from __future__ import annotations

from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


def _policy(milestones: list[dict], task_type: str = "action") -> MilestoneSupervisorPolicy:
    p = MilestoneSupervisorPolicy()
    p.task_type = task_type
    p._milestones = {m["id"]: Milestone.model_validate(m) for m in milestones}
    p._order = [m["id"] for m in milestones]
    return p


def _ms(mid, name, sc, kind="action", desc="d"):
    return {"id": mid, "name": name, "description": desc, "success_condition": sc, "kind": kind}


def test_increment_acceptance_flagged():
    p = _policy([_ms("m1", "新建机器人", "列表中新增了两台虚拟机器人")])
    issues = p._validate_decomposition("新建两台机器人")
    assert any("增量表述" in i for i in issues)


def test_terminal_acceptance_passes():
    p = _policy([_ms("m1", "新建机器人", "列表中至少有两台启用状态的虚拟机器人")])
    issues = p._validate_decomposition("新建两台机器人")
    assert not any("增量表述" in i for i in issues)


def test_intermediate_state_action_flagged():
    p = _policy([_ms("m1", "建单", "成功弹出快速建单弹窗")])
    issues = p._validate_decomposition("创建订单")
    assert any("中间态" in i for i in issues)


def test_intermediate_state_with_result_passes():
    # 「弹出弹窗并填写完成」类 —— 带了结果/完成语义,不算停在中间态
    p = _policy([_ms("m1", "建单", "提交后出现订单创建成功提示，列表中出现新订单")])
    issues = p._validate_decomposition("创建订单")
    assert not any("中间态" in i for i in issues)


def test_record_goal_without_read_step_flagged():
    p = _policy([
        _ms("m1", "进入连通性工具", "页面显示连通性检测界面", kind="navigation"),
        _ms("m2", "执行检测", "地图路径高亮或出现不可达提示"),
    ], task_type="analysis")
    issues = p._validate_decomposition("检测 A 到 C 是否可达，不可达则记录原因")
    assert any("记录/报告" in i for i in issues)


def test_record_goal_with_collection_step_passes():
    p = _policy([
        _ms("m1", "执行检测", "地图路径高亮或出现不可达提示"),
        _ms("m2", "记录检测结果", "已读取连通性判定或不可达原因", kind="collection"),
    ], task_type="analysis")
    # collection 还需 completion_strategy 合法,这里只关心 record guard 不触发
    p._milestones["m2"].completion_strategy = "read_once"
    issues = p._validate_decomposition("检测 A 到 C，不可达则记录原因")
    assert not any("记录/报告" in i for i in issues)


def test_plain_action_goal_unaffected():
    # 不含记录语义、验收为终态的普通任务:三条 guard 都不触发
    p = _policy([_ms("m1", "进入设置", "页面显示设置页", kind="navigation")])
    issues = p._validate_decomposition("打开设置页")
    assert not any(kw in i for i in issues for kw in ("增量表述", "中间态", "记录/报告"))
