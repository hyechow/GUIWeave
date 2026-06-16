"""Static turn-budget estimation for DSL orchestrator programs."""

from __future__ import annotations

from gui_agent.core.orchestrator import Cond, Finish, If, Program, Run, estimate_program_turns


def test_simple_program_stays_under_default_floor():
    prog = Program(statements=[
        Run(name="进入订单页", kind="navigation"),
        Run(name="点击刷新", kind="action"),
        Finish(message="完成"),
    ])

    assert estimate_program_turns(prog, floor=0, cap=None) < 20
    assert estimate_program_turns(prog, floor=20) == 20


def test_branch_budget_uses_longest_branch_not_sum():
    prog = Program(statements=[
        If(
            cond=Cond(var="r", field="状态", value="ok"),
            then=[
                Run(name="点击普通按钮", kind="action"),
                Run(name="上传文件", kind="action"),
            ],
            otherwise=[Run(name="点击普通按钮", kind="action")],
        ),
    ])

    estimate = estimate_program_turns(prog, floor=0, cap=None)
    summed_branches = estimate_program_turns(
        Program(statements=[
            Run(name="点击普通按钮", kind="action"),
            Run(name="上传文件", kind="action"),
            Run(name="点击普通按钮", kind="action"),
        ]),
        floor=0,
        cap=None,
    )
    assert estimate < summed_branches


def test_complex_order_action_expands_budget():
    prog = Program(statements=[
        Run(name="确保已登录并处于首页", kind="navigation", precondition=True),
        Run(name="进入路径连通性工具页面", kind="navigation"),
        Run(name="设置起点、终点并触发检测", kind="action"),
        Run(var="r", name="读取路径连通性检测结果", kind="read", returns=["是否连通"]),
        If(
            cond=Cond(var="r", field="是否连通", value="连通"),
            then=[
                Run(name="进入虚拟机器人列表页面", kind="navigation"),
                Run(var="t", name="读取第一个虚拟机器人的名称", kind="read", returns=["机器人名称"]),
                Run(name="进入订单列表页面", kind="navigation"),
                Run(
                    name="创建移动订单（机器人={t[机器人名称]}, 动作序列：移动到 s10 -> 移动到 s9）",
                    kind="action",
                ),
                Run(var="o", name="确认订单创建结果", kind="read", returns=["订单状态"]),
                Finish(message="{o[订单状态]}"),
            ],
            otherwise=[Finish(message="不可达")],
        ),
    ])

    # Calibrated against real run 20260616_092555 (this exact shape ran to / starved at 20):
    # it rises above the floor (a create-order chain genuinely needs headroom) but stays well
    # under the cap (32) — the old per-statement model over-budgeted the same program to 41.
    estimate = estimate_program_turns(prog, floor=20)
    assert 20 < estimate <= 32


def test_cap_clamps_large_program_down():
    # A program whose raw estimate blows past the cap is clamped DOWN to cap (the runaway
    # ceiling), while floor stays the requested minimum — the other edge from
    # test_floor_is_never_lowered_by_cap (there floor > cap; here floor < cap < estimate).
    prog = Program(statements=[
        Run(name=f"创建订单并配置动作序列 {i}", kind="action")
        for i in range(10)
    ])

    assert estimate_program_turns(prog, floor=0, cap=None) > 45   # raw estimate exceeds cap
    assert estimate_program_turns(prog, floor=20, cap=45) == 45   # clamped down to cap


def test_floor_is_never_lowered_by_cap():
    prog = Program(statements=[
        Run(name=f"创建订单并配置动作序列 {i}", kind="action")
        for i in range(10)
    ])

    assert estimate_program_turns(prog, floor=60, cap=45) == 60
