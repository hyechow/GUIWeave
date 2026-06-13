"""verification 误标拯救：带读取/记录产出的「verification」子目标是任务脉络的终读步，
_patch_decomposition 一刀切删除会吞掉它（查看订单状态/记录检测结果,~30% 分解中招,
回归 20260612 连通性 eval 3/10 丢「记录」步）。修法=救成 collection/read_once；
纯验证项（验证 X 已完成,无读取产出）照删。
"""

from __future__ import annotations

from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


def _policy(milestones: list[dict]) -> MilestoneSupervisorPolicy:
    p = MilestoneSupervisorPolicy()
    p.task_type = "action"
    p._milestones = {m["id"]: Milestone.model_validate(m) for m in milestones}
    p._order = [m["id"] for m in milestones]
    return p


def _ms(mid, name, sc, kind="action", desc="d", depends=None):
    return {
        "id": mid, "name": name, "description": desc,
        "success_condition": sc, "kind": kind, "depends_on": depends or [],
    }


def test_record_verification_rescued_as_collection():
    p = _policy([
        _ms("m1", "执行路径连通性检测", "页面显示检测结果", kind="action"),
        _ms("m2", "记录检测结果", "明确识别出结果为「连通」或「路径不可达」", kind="verification", depends=["m1"]),
    ])
    p._patch_decomposition(None, "检测连通性并记录检测结果")
    assert "m2" in p._milestones
    m2 = p._milestones["m2"]
    assert m2.kind == "collection"
    assert m2.completion_strategy == "read_once"
    assert m2.depends_on == ["m1"]


def test_view_status_verification_rescued():
    p = _policy([
        _ms("m1", "创建移动订单", "订单列表出现新订单", kind="action"),
        _ms("m2", "查看订单执行状态", "订单状态清晰可见", kind="verification", depends=["m1"]),
    ])
    p._patch_decomposition(None, "建单后查看订单执行状态")
    assert "m2" in p._milestones
    assert p._milestones["m2"].kind == "collection"


def test_report_alias_lands_verification_then_rescued():
    # LLM 把「记录/汇报」步标成 kind=report → schema alias 归一成 verification → 须被拯救
    m = Milestone.model_validate(_ms("m2", "记录检测结果", "已读出判定", kind="report"))
    assert m.kind == "verification"
    p = _policy([
        _ms("m1", "执行检测", "页面显示结果", kind="action"),
        _ms("m2", "记录检测结果", "已读出判定", kind="report", depends=["m1"]),
    ])
    p._patch_decomposition(None, "检测并记录结果")
    assert "m2" in p._milestones
    assert p._milestones["m2"].kind == "collection"


def test_pure_verification_still_removed_with_dep_splice():
    p = _policy([
        _ms("m1", "创建虚拟机器人", "列表出现新条目", kind="action"),
        _ms("m2", "验证机器人已创建", "列表中可见该条目且状态为启用", kind="verification", depends=["m1"]),
        _ms("m3", "下发移动订单", "订单列表出现新订单", kind="action", depends=["m2"]),
    ])
    p._patch_decomposition(None, "创建机器人并下单")
    assert "m2" not in p._milestones          # 纯验证（无读取/记录产出）照删
    assert p._milestones["m3"].depends_on == ["m1"]  # 依赖剪接到被删项的前置
