"""DSL orchestrator MVP: the runner interprets a milestone-level program and threads
each run()'s structured result through variables / conditions / finish.

No GUI, no LLM: the linear (single-milestone) executor is mocked so we test the
interpreter's control flow, variable env, reads persistence, and finish templating.
The driving scenario is the connectivity branch (检测→读判定→if 连通 建单 else finish 原因).
"""

from __future__ import annotations

from gui_agent.core.orchestrator import (
    Cond,
    Finish,
    If,
    Program,
    ProgramRunner,
    Run,
    RunResult,
)


def _connectivity_program() -> Program:
    return Program(
        goal="检测 s10→s9 连通性；连通则建单，不可达则记录原因",
        statements=[
            Run(name="进入路径连通性工具页", kind="navigation"),
            Run(name="设置起点 s10、终点 s9", kind="filter"),
            Run(name="点击检测按钮执行连通性检测", kind="action"),
            Run(var="d", name="读取连通判定结果", kind="read",
                returns=["连通判定", "不可达原因"]),
            If(
                cond=Cond(var="d", field="连通判定", value="连通"),
                then=[
                    Run(name="进入订单列表页", kind="navigation"),
                    Run(name="建单 s10→s9", kind="action"),
                ],
                otherwise=[Finish(message="路径不可达，原因：{d[不可达原因]}")],
            ),
        ],
    )


def _driver(verdict: str, reason: str = ""):
    """Mock single-milestone executor: the read milestone (var d) returns the verdict."""
    def _exec(run: Run) -> RunResult:
        reads = {"连通判定": verdict, "不可达原因": reason} if run.var == "d" else {}
        return RunResult(completed=True, failed=False, reads=reads, summary=run.name)
    return _exec


def test_connected_branch_creates_order():
    res = ProgramRunner(_driver("连通")).run(_connectivity_program())
    assert res.failed is False
    # 连通支：建单 run 被执行
    assert any(r.name == "建单 s10→s9" for r in res.run_log)
    # 没走 finish 的不可达文案
    assert "不可达" not in res.reply
    # 变量环境持久化了读取结果（解决「中间读到了输出不知道」）
    assert res.env["d"].reads["连通判定"] == "连通"


def test_unreachable_branch_finishes_with_reason():
    res = ProgramRunner(_driver("不可达", reason="终点 s9 被占用")).run(_connectivity_program())
    # finish 模板用 env 填充 {d[不可达原因]}
    assert res.reply == "路径不可达，原因：终点 s9 被占用"
    # 建单支没有执行
    assert not any(r.name == "建单 s10→s9" for r in res.run_log)


def test_reads_persist_across_runs():
    # 多个 run 的 reads 都进 env / run_log，不只看最后一个。
    prog = Program(statements=[
        Run(var="a", name="读 A", kind="read", returns=["x"]),
        Run(var="b", name="读 B", kind="read", returns=["y"]),
    ])
    def _exec(run: Run) -> RunResult:
        m = {"a": {"x": "X值"}, "b": {"y": "Y值"}}
        return RunResult(completed=True, reads=m.get(run.var or "", {}), summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert res.env["a"].reads["x"] == "X值" and res.env["b"].reads["y"] == "Y值"
    # 无 finish → auto summary 从 run_log 汇总两个读取值
    assert "X值" in res.reply and "Y值" in res.reply


def test_failed_run_stops_and_reports():
    prog = Program(statements=[
        Run(name="会失败的步骤", kind="action"),
        Run(name="不该执行的后续", kind="action"),
    ])
    def _exec(run: Run) -> RunResult:
        if run.name == "会失败的步骤":
            return RunResult(completed=False, failed=True, summary="控件不可点")
        return RunResult(completed=True)
    res = ProgramRunner(_exec).run(prog)
    assert res.failed is True
    assert "会失败的步骤" in res.reply
    assert not any(r.name == "不该执行的后续" for r in res.run_log)


def test_confirm_read_after_action_drives_final_answer():
    # confirm-read 模式：状态变更动作后跟一个 read 结构化确认结果，finish 据它答复——
    # 成败不再只信动作步被判完成（checker 可能幻觉），而是由 confirm read 的结构化值定。
    prog = Program(statements=[
        Run(name="建单 s10→s9", kind="action"),
        Run(var="c", name="确认订单已创建", kind="read", returns=["建单结果"],
            read_spec="建单结果：订单列表出现该行或成功提示→成功，否则失败"),
        Finish(message="建单结果：{c[建单结果]}"),
    ])
    def _exec(run: Run) -> RunResult:
        reads = {"建单结果": "成功"} if run.var == "c" else {}
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert res.reply == "建单结果：成功"          # 答复由 confirm read 的结构化值驱动
    assert res.env["c"].reads["建单结果"] == "成功"  # confirm 结果进了 env


def test_neq_condition():
    prog = Program(statements=[
        Run(var="d", name="读状态", kind="read", returns=["状态"]),
        If(cond=Cond(var="d", field="状态", cmp="!=", value="正常"),
           then=[Finish(message="异常：{d[状态]}")],
           otherwise=[Finish(message="一切正常")]),
    ])
    res = ProgramRunner(lambda r: RunResult(completed=True, reads={"状态": "告警"})).run(prog)
    assert res.reply == "异常：告警"


def test_extended_condition_operators():
    def _reply(reads: dict[str, str], cond: Cond) -> str:
        prog = Program(statements=[
            Run(var="r", name="读状态", kind="read", returns=["状态", "提示", "订单号", "错误"]),
            If(cond=cond, then=[Finish(message="then")], otherwise=[Finish(message="else")]),
        ])
        res = ProgramRunner(lambda r: RunResult(completed=True, reads=reads)).run(prog)
        return res.reply

    assert _reply({"订单号": "ORD-001"}, Cond(var="r", field="订单号", cmp="exists")) == "then"
    assert _reply({"订单号": ""}, Cond(var="r", field="订单号", cmp="empty")) == "then"
    assert _reply(
        {"提示": "订单创建成功，编号 ORD-001"},
        Cond(var="r", field="提示", cmp="contains", value="创建成功"),
    ) == "then"
    assert _reply(
        {"提示": "订单创建成功，编号 ORD-001"},
        Cond(var="r", field="提示", cmp="not_contains", value="失败"),
    ) == "then"
    assert _reply(
        {"状态": "进行中"},
        Cond(var="r", field="状态", cmp="in", values=["待执行", "进行中"]),
    ) == "then"
    assert _reply(
        {"状态": "进行中"},
        Cond(var="r", field="状态", cmp="not_in", values=["失败", "已取消"]),
    ) == "then"


# ── 可步进生成器接口（agent_loop 集成时就这么驱动）──────────────────────────────


def test_steppable_interpreter_yields_runs_and_consumes_results():
    from gui_agent.core.orchestrator import Interpreter
    interp = Interpreter(_connectivity_program())
    gen = interp.steps()
    driven: list[str] = []
    run = next(gen)
    reply = None
    while True:
        driven.append(run.name)
        # 把 read milestone 判成不可达 → 走 finish 支
        reads = {"连通判定": "不可达", "不可达原因": "节点离线"} if run.var == "d" else {}
        result = RunResult(completed=True, reads=reads, summary=run.name)
        try:
            run = gen.send(result)
        except StopIteration as e:
            reply = e.value
            break
    # engine 只被要求驱动到决策点为止；建单支没被 yield（生成器按条件只产命中分支）
    assert "读取连通判定结果" in driven
    assert "建单 s10→s9" not in driven
    assert reply == "路径不可达，原因：节点离线"
    # env/run_log 在生成器对象上可读，供 agent_loop 汇总
    assert interp.env["d"].reads["连通判定"] == "不可达"
    assert any(r.name == "读取连通判定结果" for r in interp.run_log)


def test_steppable_program_with_only_finish():
    from gui_agent.core.orchestrator import Interpreter
    interp = Interpreter(Program(statements=[Finish(message="直接结束")]))
    gen = interp.steps()
    # 没有 run() → 第一个 next 就 StopIteration，value=reply
    import pytest
    with pytest.raises(StopIteration) as ei:
        next(gen)
    assert ei.value.value == "直接结束"


# ── engine glue: Run -> Milestone / task_type / RunResult packaging ──────────────


def test_to_milestone_maps_runkind():
    from gui_agent.core.orchestrator.engine import to_milestone
    nav = to_milestone(Run(name="进入页", kind="navigation"), 0)
    assert nav.kind == "navigation" and nav.completion_strategy == "visible_once"
    rd = to_milestone(Run(var="d", name="读结果", kind="read", returns=["连通判定", "原因"]), 3)
    assert rd.kind == "collection" and rd.completion_strategy == "read_once"
    assert rd.id == "d"                              # var → milestone id
    assert "连通判定" in rd.description and "原因" in rd.description  # returns 折进 description
    dq = to_milestone(Run(var="q", name="统计订单", kind="data_query", returns=["emails"], sql="SELECT 1"), 4)
    assert dq.kind == "collection" and dq.completion_strategy == "read_once"
    assert dq.id == "q"


def test_task_type_for_non_ui_is_analysis():
    from gui_agent.core.orchestrator.engine import task_type_for
    assert task_type_for(Run(name="读", kind="read", returns=["x"])) == "analysis"
    assert task_type_for(Run(name="查", kind="data_query", returns=["x"], sql="SELECT 1")) == "analysis"
    assert task_type_for(Run(name="点", kind="action")) == "action"


def test_package_result_contract():
    from gui_agent.core.orchestrator.engine import package_result
    ok = package_result(Run(name="x", kind="action"), completed=True, summary="成功", notes=["证据1"])
    assert ok.completed and not ok.failed and ok.evidence == ["证据1"]
    bad = package_result(Run(name="x", kind="action"), completed=False, summary="失败", notes=[])
    assert bad.failed and not bad.completed


def test_supervisor_reseed_single_milestone():
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
    from gui_agent.core.orchestrator.engine import to_milestone, task_type_for
    p = MilestoneSupervisorPolicy()
    p._milestones = {"old": object()}  # type: ignore[dict-item]
    p._order = ["old", "x"]
    p._recent_screenshots.append((b"frame", None))  # stuck 检测器帧历史
    p._scroll_counts = {"old": 5}             # 其余 per-milestone 态
    run = Run(var="d", name="读判定", kind="read", returns=["连通判定"])
    p.reseed(to_milestone(run, 0), task_type=task_type_for(run))
    assert list(p._order) == ["d"] and p._current_id == "d"
    assert p.task_type == "analysis"          # read → analysis（读取门）
    # 与 DAG 的 _advance 对齐：只清 _recent_screenshots，其余跨 milestone 态保留（不再过度清空）。
    assert list(p._recent_screenshots) == []
    assert p._scroll_counts == {"old": 5}


def test_reseed_fresh_advance_nav_skips_initial_check():
    # DAG _advance parity: a freshly-advanced NAVIGATION milestone skips its first done-check
    # (in_progress by construction); action/filter keep it; a non-fresh reseed never skips.
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
    from gui_agent.core.orchestrator.engine import to_milestone
    p = MilestoneSupervisorPolicy()
    p.reseed(to_milestone(Run(name="进页", kind="navigation"), 0), fresh_advance=True)
    assert p._skip_initial_check is True                  # nav + 刚推进 → 跳 check
    p.reseed(to_milestone(Run(name="点按钮", kind="action"), 1), fresh_advance=True)
    assert p._skip_initial_check is False                 # action → 保留 check（防双执行）
    p.reseed(to_milestone(Run(name="进页", kind="navigation"), 2), fresh_advance=False)
    assert p._skip_initial_check is False                 # 非交接（如首个 milestone）→ 不跳


def test_advance_persists_done_check_on_terminal_completion():
    # The report's 验收 panel renders context.milestone_states[id].done_check (sourced from the
    # supervisor runtime snapshot). A single-milestone (orchestrator) completion hits _advance's
    # TERMINAL branch — it must still save the done verdict, else the panel is empty (regression
    # seen in 20260615_113554 after the hand-off merge).
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
    from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
    from gui_agent.core.orchestrator.engine import to_milestone
    from gui_agent.core.schemas import Observation
    p = MilestoneSupervisorPolicy()
    ms = to_milestone(Run(name="进首页", kind="navigation"), 0)
    p.reseed(ms)                                          # single milestone, orchestrator style
    check = _SingleCheckResult(status="done", reason="已进入首页", summary="首页")
    p._last_check = check
    step = p._advance(ms, Observation(png_bytes=b"x", source="test"), [])
    assert step.goal_completed is True                    # single milestone → terminal step
    assert p._milestone_done_checks[ms.id] is check       # done 判定已留存（验收面板有数据）


# ── #3 structured read: reads 进 RunResult，让 if 真分支 ──────────────────────────


def test_package_result_carries_structured_reads():
    from gui_agent.core.orchestrator.engine import package_result
    r = package_result(Run(var="d", name="读", kind="read", returns=["连通判定"]),
                       completed=True, summary="读完", notes=[],
                       reads={"连通判定": "连通"})
    assert r.reads == {"连通判定": "连通"} and r.completed


def test_structured_read_empty_returns_no_llm():
    # 无 returns 直接返回 {}，不触 LLM（确定性）。read_spec/check_knowledge 都不影响。
    from gui_agent.core.orchestrator.structured_read import structured_read
    assert structured_read(b"x", [], read_spec="任务说明", check_knowledge="线索") == {}


def test_normalize_confirm_read_gates_rewrites_action_before_read():
    # L2 backstop: an action immediately followed by a read (confirm-read) gets a lenient
    # DISPATCH success_condition, so the checker accepts on "fired" not on the result.
    from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates
    prog = Program(statements=[
        Run(name="进页", kind="navigation", success_condition="页面已显示"),
        Run(name="设起终点并执行检测", kind="action",
            success_condition="检测结果（连通标记或不可达提示）已显示在界面"),  # result gate
        Run(var="r", name="读连通", kind="read", returns=["连通状态"], read_spec="看绿✓"),
        Finish(message="{r[连通状态]}"),
    ])
    out = normalize_confirm_read_gates(prog)
    nav, act, read, fin = out.statements
    # 触发型 action 的验收被改写成 dispatch 门：不再断言结果，明确让位给 read
    assert "不判定结果取值" in act.success_condition
    assert "下一步读取判定" in act.success_condition
    assert "连通标记" not in act.success_condition  # 结果门措辞已被替换
    # navigation / read / finish 不动；read 的 returns/read_spec 原样
    assert nav.success_condition == "页面已显示"
    assert read.returns == ["连通状态"] and read.read_spec == "看绿✓"
    # 原 Program 不被就地改（返回新对象）
    assert prog.statements[1].success_condition == "检测结果（连通标记或不可达提示）已显示在界面"


def test_normalize_confirm_read_gates_recurses_into_if_branches():
    # confirm-read inside an if-branch (建单 action → 确认建单 read) is rewritten too;
    # the otherwise branch (no action→read pair) is untouched.
    from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates
    prog = Program(statements=[
        Run(var="r", name="读判定", kind="read", returns=["是否可达"], read_spec="x"),
        If(cond=Cond(var="r", field="是否可达", value="可达"),
           then=[
               Run(name="建单", kind="action", success_condition="订单创建成功提示"),  # 后跟 confirm-read
               Run(var="c", name="确认建单", kind="read", returns=["建单结果"], read_spec="y"),
               Finish(message="{c[建单结果]}"),
           ],
           otherwise=[Finish(message="不可达")]),
    ])
    out = normalize_confirm_read_gates(prog)
    then_action = out.statements[1].then[0]
    assert "不判定结果取值" in then_action.success_condition
    assert "订单创建成功" not in then_action.success_condition  # 原结果门措辞被替换
    assert out.statements[1].otherwise[0].message == "不可达"  # otherwise 不受影响


def test_normalize_confirm_read_converts_filter_before_read_to_action():
    # A filter whose only purpose is to produce a value for the next read is a trigger, not
    # the final acceptance target. Convert it to action so the filter checker doesn't
    # re-judge rows/counts that the read owns (WebArena admin grid count tasks).
    from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates
    prog = Program(statements=[
        Run(name="提交 Review 列关键词 best 的筛选", kind="filter",
            success_condition="列表只显示 Review 包含 best 的记录"),
        Run(var="r", name="读取评论总数", kind="read", returns=["总数"],
            read_spec="总数：读取 grid 顶部 N records found 中的 N"),
    ])
    out = normalize_confirm_read_gates(prog)
    trigger, read = out.statements
    assert trigger.kind == "action"
    assert "不判定结果取值" in trigger.success_condition
    assert "列表只显示" not in trigger.success_condition
    assert read.kind == "read" and read.returns == ["总数"]
    assert prog.statements[0].kind == "filter"  # 原 Program 不就地改


def test_normalize_confirm_read_converts_filter_before_data_query_to_action():
    from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates
    prog = Program(statements=[
        Run(name="提交订单筛选", kind="filter", success_condition="列表只显示匹配订单"),
        Run(var="q", name="统计邮箱", kind="data_query", returns=["emails"],
            sql="SELECT customer_email FROM data"),
    ])
    out = normalize_confirm_read_gates(prog)
    trigger, query = out.statements
    assert trigger.kind == "action"
    assert "不判定结果取值" in trigger.success_condition
    assert query.kind == "data_query" and query.returns == ["emails"]


def test_normalize_confirm_read_converts_filter_inside_if_branch():
    from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates
    prog = Program(statements=[
        Run(var="d", name="读判定", kind="read", returns=["需要查询"], read_spec="x"),
        If(cond=Cond(var="d", field="需要查询", value="是"),
           then=[
               Run(name="提交搜索条件", kind="filter", success_condition="搜索结果均匹配条件"),
               Run(var="r", name="读取结果数", kind="read", returns=["结果数"], read_spec="读 records found"),
           ],
           otherwise=[Finish(message="无需查询")]),
    ])
    out = normalize_confirm_read_gates(prog)
    then_filter = out.statements[1].then[0]
    assert then_filter.kind == "action"
    assert "不判定结果取值" in then_filter.success_condition
    assert out.statements[1].otherwise[0].message == "无需查询"


def test_normalize_precondition_gates_is_flag_based_not_keyword():
    # The L2 detection is the STRUCTURAL run.precondition flag, NOT name keywords. A flagged step
    # (login precondition with the 153314 form antipattern) gets the generic ensure-state gate; a
    # step that merely MENTIONS 登录 but isn't a precondition (查询登录日志) is left alone — the old
    # keyword pass would have mis-rewritten it. App-specific markers stay in _check.md.
    from gui_agent.core.orchestrator.engine import normalize_precondition_gates
    prog = Program(statements=[
        Run(name="确保已登录 RoboTeam", kind="navigation", precondition=True,
            success_condition="页面显示账号、密码输入框及登录按钮"),     # flagged + form antipattern
        Run(name="查询登录日志", kind="action",
            success_condition="显示登录日志列表"),                       # 含「登录」但不是前置、未标 flag
    ])
    out = normalize_precondition_gates(prog)
    assert "已处于该前置步要求的目标状态" in out.statements[0].success_condition  # flagged → 通用门
    assert out.statements[0].success_condition != prog.statements[0].success_condition
    assert out.statements[1].success_condition == "显示登录日志列表"  # 含「登录」但没标 → 不动（旧关键词版会误改）
    assert prog.statements[0].success_condition == "页面显示账号、密码输入框及登录按钮"  # 原对象未就地改


def test_normalize_precondition_gates_recurses_and_idempotent():
    from gui_agent.core.orchestrator.engine import normalize_precondition_gates
    prog = Program(statements=[
        Run(var="d", name="读判定", kind="read", returns=["x"], read_spec="y"),
        If(cond=Cond(var="d", field="x", value="1"),
           then=[Run(name="确保已进入编辑模式", kind="navigation", precondition=True,
                     success_condition="看到编辑面板")],
           otherwise=[Run(name="退出编辑", kind="action", success_condition="返回列表")]),
    ])
    out = normalize_precondition_gates(prog)
    assert "已处于该前置步要求的目标状态" in out.statements[1].then[0].success_condition  # if 分支内前置被改写
    assert out.statements[1].otherwise[0].success_condition == "返回列表"                 # 非前置步不动
    # 幂等
    assert normalize_precondition_gates(out).statements[1].then[0].success_condition \
        == out.statements[1].then[0].success_condition


def test_normalize_confirm_read_gates_action_without_following_read_unchanged():
    # 真正「action 后面不是 read」的场景：两个连续 action，第一个不应被改写。
    from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates
    prog = Program(statements=[
        Run(name="第一步动作", kind="action", success_condition="第一步生效页"),
        Run(name="第二步动作", kind="action", success_condition="第二步生效页"),
    ])
    out = normalize_confirm_read_gates(prog)
    assert out.statements[0].success_condition == "第一步生效页"
    assert out.statements[1].success_condition == "第二步生效页"
    # 幂等：再跑一次不变
    assert normalize_confirm_read_gates(out).statements[0].success_condition == "第一步生效页"


def test_if_branches_on_structured_reads_end_to_end():
    # 串起来：read milestone 拿到结构化 {连通判定:连通} → if 走 then(建单)。
    prog = Program(statements=[
        Run(var="d", name="读连通判定", kind="read", returns=["连通判定"]),
        If(cond=Cond(var="d", field="连通判定", value="连通"),
           then=[Run(name="建单", kind="action")],
           otherwise=[Finish(message="不可达")]),
    ])
    def _exec(run: Run) -> RunResult:
        reads = {"连通判定": "连通"} if run.var == "d" else {}
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert any(r.name == "建单" for r in res.run_log)   # 连通 → 建单（结构化 reads 驱动分支）
    assert res.reply != "不可达"


# ── read-then-reference: a prior read's value serves the NEXT action's target ───────


def test_run_text_templated_from_prior_read_reaches_executor_filled():
    # read-then-reference (规则9/runner._fill, 回归 20260615_163258): an action authored as
    # 『编辑机器人 {r[实际名称]}』must reach the per-milestone executor ALREADY filled with the
    # value a prior read captured (编辑机器人 lucas-10003) — so the planner targets the right
    # entity even when the list holds siblings, not just whatever single row is on screen.
    prog = Program(statements=[
        Run(name="按配置新建机器人", kind="action"),
        Run(var="r", name="读取实际名称", kind="read", returns=["实际名称"],
            read_spec="读列表新增行名称"),
        Run(name="编辑机器人 {r[实际名称]}，设预设站点 s10", kind="action",
            success_condition="{r[实际名称]} 的预设站点已为 s10"),
    ])
    seen: list[Run] = []
    def _exec(run: Run) -> RunResult:
        seen.append(run)
        reads = {"实际名称": "lucas-10003"} if run.var == "r" else {}
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    edit = seen[-1]
    assert edit.name == "编辑机器人 lucas-10003，设预设站点 s10"   # {r[实际名称]} 已被 env 填好
    assert edit.success_condition == "lucas-10003 的预设站点已为 s10"
    assert not any("{r[" in r.name for r in seen)               # 无未解析模板漏到执行器
    assert any(r.name == "编辑机器人 lucas-10003，设预设站点 s10" for r in res.run_log)


def test_run_without_refs_is_not_copied():
    # 无 {var[字段]} 引用的 run 原样 yield（_fill 早返回，不做无谓 copy）。
    from gui_agent.core.orchestrator.runner import Interpreter
    interp = Interpreter(Program(statements=[Run(name="点按钮", kind="action")]))
    gen = interp.steps()
    yielded = next(gen)
    assert yielded.name == "点按钮"


def test_run_target_template_empty_value_fails_fast_not_silent_gap():
    # 目标字段（name）的 {var[字段]} 在运行时读到空（read 读不到=当没有）→ 不能带空指代驱动动作，
    # 应 fail-fast 诚实报错，而不是把『编辑机器人 ，设…』静默送给 planner。
    prog = Program(statements=[
        Run(var="r", name="读实际名称", kind="read", returns=["实际名称"], read_spec="x"),
        Run(name="编辑机器人 {r[实际名称]}，设预设站点 s10", kind="action"),
    ])
    seen: list[str] = []
    def _exec(run: Run) -> RunResult:
        seen.append(run.name)
        reads = {"实际名称": ""} if run.var == "r" else {}   # read 读不到该值
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert res.failed is True                                  # 程序诚实失败
    assert not any("编辑机器人" in n for n in seen)            # 空指代的 action 没被驱动
    assert "实际名称" in res.reply                            # 报清是哪个引用空了


def test_run_acceptance_gate_template_empty_is_lenient():
    # 只有验收门（success_condition）的模板空，name 是具体的 → 动作目标没歧义，不该 fail-fast，
    # 门弱化可接受（与 finish 一样宽松）。
    prog = Program(statements=[
        Run(var="r", name="读名称", kind="read", returns=["名称"], read_spec="x"),
        Run(name="点击保存", kind="action", success_condition="{r[名称]} 已保存"),  # 仅门里有引用
    ])
    seen: list[str] = []
    def _exec(run: Run) -> RunResult:
        seen.append(run.name)
        return RunResult(completed=True, reads={"名称": ""} if run.var == "r" else {}, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert res.failed is False
    assert "点击保存" in seen                                  # name 具体 → 照常驱动


def test_validate_program_flags_forward_and_cross_branch_refs():
    # Finding 2：校验是路径敏感的，不是全局符号表——引用必须在「它之前、当前路径上已执行」的 read。
    from gui_agent.core.orchestrator.decomposer import validate_program
    # ① forward：先引用、后读取
    forward = Program(statements=[
        Run(name="编辑 {r[名称]}", kind="action"),
        Run(var="r", name="读名称", kind="read", returns=["名称"], read_spec="x"),
    ])
    assert any("尚未产生" in i for i in validate_program(forward))
    # ② cross-branch：一个分支读、另一个分支引用
    cross = Program(statements=[
        Run(var="d", name="读判定", kind="read", returns=["判定"], read_spec="x"),
        If(cond=Cond(var="d", field="判定", value="A"),
           then=[Run(var="r", name="读名称", kind="read", returns=["名称"], read_spec="x")],
           otherwise=[Run(name="编辑 {r[名称]}", kind="action")]),
    ])
    assert any("尚未产生" in i for i in validate_program(cross))
    # ③ 两支都产生同一 var/字段 → 汇合后在 if 之后引用合法（dominance join）
    merged = Program(statements=[
        Run(var="d", name="读判定", kind="read", returns=["判定"], read_spec="x"),
        If(cond=Cond(var="d", field="判定", value="A"),
           then=[Run(var="r", name="读名A", kind="read", returns=["名称"], read_spec="x")],
           otherwise=[Run(var="r", name="读名B", kind="read", returns=["名称"], read_spec="x")]),
        Run(name="编辑 {r[名称]}", kind="action", success_condition="完成"),
    ])
    assert validate_program(merged) == []


def test_validate_program_flags_dangling_run_ref():
    from gui_agent.core.orchestrator.decomposer import validate_program
    # action 引用了 r 没 returns 的字段 → 悬空（会填空）
    bad_field = Program(statements=[
        Run(var="r", name="读名称", kind="read", returns=["实际名称"], read_spec="x"),
        Run(name="编辑 {r[不存在字段]}", kind="action"),
    ])
    assert any("不存在字段" in i for i in validate_program(bad_field))
    # 引用了不是任何 read 步 var 的变量 q → 指代落空
    bad_var = Program(statements=[
        Run(name="编辑 {q[名称]}", kind="action"),
        Run(var="r", name="读", kind="read", returns=["名称"], read_spec="x"),
    ])
    assert any("q" in i and "落空" in i for i in validate_program(bad_var))
    # 合法的 read-then-reference 不报悬空
    good = Program(statements=[
        Run(var="r", name="读名称", kind="read", returns=["实际名称"], read_spec="x"),
        Run(name="编辑机器人 {r[实际名称]}", kind="action", success_condition="完成"),
    ])
    assert validate_program(good) == []


def test_validate_program_flags_dangling_finish_ref():
    # docstring 一直声称「finish {var[field]} ref must resolve」，现在真校验了。
    from gui_agent.core.orchestrator.decomposer import validate_program
    prog = Program(statements=[
        Run(var="r", name="读", kind="read", returns=["状态"], read_spec="x"),
        Finish(message="结果：{r[不存在]}"),
    ])
    assert any("不存在" in i for i in validate_program(prog))


def test_validate_program_flags_bare_var_ref():
    # 回归 20260615_194320：分解器写了裸 {robot_name}（缺 [字段]），既填不进值又逃过模板解析，
    # 字面量漏给 planner。校验要抓：{var} 里 var 是已知 read 的 var 却缺 [字段] → 坏引用、反馈重试。
    from gui_agent.core.orchestrator.decomposer import validate_program
    bad = Program(statements=[
        Run(var="robot_name", name="读机器人名", kind="read", returns=["机器人名称"], read_spec="x"),
        Run(name="编辑机器人 {robot_name}", kind="action", success_condition="完成"),
    ])
    assert any("裸" in i and "robot_name" in i for i in validate_program(bad))
    # 正确的 {var[field]} 不被裸校验误报；不是 read var 的 {x} 也不报（只盯已知 read var）
    ok = Program(statements=[
        Run(var="r", name="读名", kind="read", returns=["实际名称"], read_spec="x"),
        Run(name="编辑 {r[实际名称]}，温度 {x} 档", kind="action", success_condition="完成"),
    ])
    assert not any("裸" in i for i in validate_program(ok))


def test_validate_program_branch_join_uses_field_intersection():
    # Finding 5（分支汇合字段交集 bug）：两支都产出同一 var 但 returns 字段不同 → 汇合后只有
    # 【双支共有字段】才保证存在；引用一支独有的字段必须被抓——旧版用字段【并集】会放过它，运行
    # 时走到另一分支该字段缺失、模板静默填空（{r[名称]} 在只 returns 编号的那条路上落空）。
    from gui_agent.core.orchestrator.decomposer import validate_program
    diverge = Program(statements=[
        Run(var="d", name="读判定", kind="read", returns=["判定"], read_spec="x"),
        If(cond=Cond(var="d", field="判定", value="A"),
           then=[Run(var="r", name="读名称", kind="read", returns=["名称"], read_spec="x")],
           otherwise=[Run(var="r", name="读编号", kind="read", returns=["编号"], read_spec="x")]),
        Run(name="编辑 {r[名称]}", kind="action", success_condition="完成"),  # 名称只有 then 支产出
    ])
    assert any("名称" in i for i in validate_program(diverge))   # 字段并集会漏报，交集抓住
    # 双支共有字段（都 returns「标识」）则放过；各自独有的字段不进 scope
    common = Program(statements=[
        Run(var="d", name="读判定", kind="read", returns=["判定"], read_spec="x"),
        If(cond=Cond(var="d", field="判定", value="A"),
           then=[Run(var="r", name="读A", kind="read", returns=["标识", "名称"], read_spec="x")],
           otherwise=[Run(var="r", name="读B", kind="read", returns=["标识", "编号"], read_spec="x")]),
        Run(name="编辑 {r[标识]}", kind="action", success_condition="完成"),  # 标识双支都有 → 合法
    ])
    assert validate_program(common) == []


def test_validate_program_precondition_only_on_navigation():
    # Finding 3（precondition 硬校验）：precondition=true 是「确保已处于某状态」，只对 navigation 步
    # 有意义。误标在 action/read/filter 上会被 L2 当「已满足」首帧判完成 → 该真正执行的步被跳过。
    from gui_agent.core.orchestrator.decomposer import validate_program
    bad = Program(statements=[
        Run(name="提交订单", kind="action", precondition=True, success_condition="已提交"),
    ])
    assert any("precondition" in i and "navigation" in i for i in validate_program(bad))
    # navigation 前置不报；普通 navigation（未标 flag）也不报
    good = Program(statements=[
        Run(name="确保已登录", kind="navigation", precondition=True, success_condition=""),
        Run(name="打开订单页", kind="navigation", success_condition="显示订单列表"),
    ])
    assert not any("precondition" in i for i in validate_program(good))


# ── empty-read guard: a finish answered on an entirely-empty read is incomplete ───


def test_finish_on_entirely_empty_read_is_incomplete():
    # WebArena #42 shape: a single read whose only field came back "" (target table was
    # off-screen / wrong page) feeds a finish. The program reached the end, but its answer
    # is hollow → finish_incomplete must be True so goal_completed stays False (result.py).
    prog = Program(statements=[
        Run(var="r", name="读取前2个搜索词", kind="read", returns=["Top Search Terms"],
            read_spec="读 Last Search Terms 表格前两行"),
        Finish(message="商店的前 2 个搜索词是：{r[Top Search Terms]}"),
    ])
    def _exec(run: Run) -> RunResult:
        reads = {"Top Search Terms": ""} if run.var == "r" else {}   # 读不到 → 空
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert res.finish_incomplete is True          # 整条 read 全空 → 不算答出
    assert res.failed is False                    # 但程序确实跑完了（区别于 milestone 失败）


def test_finish_citing_blank_field_of_nonempty_read_not_incomplete():
    # Guard is WHOLE-read, not per-ref: a multi-field read where only ONE field is blank
    # (合法: 如 连通判定=可达 时 不可达原因 留空) is NOT entirely empty → a finish citing that
    # blank field must NOT be flagged. This protects the otherwise-branch finish
    # "不可达原因：{d[不可达原因]}" from a per-ref rule that would mis-kill it.
    prog = Program(statements=[
        Run(var="d", name="读连通判定", kind="read", returns=["连通判定", "不可达原因"],
            read_spec="连通判定看图标；不可达原因可达时留空"),
        Finish(message="不可达原因：{d[不可达原因]}"),   # 引用的恰好是空字段，但另一字段非空
    ])
    def _exec(run: Run) -> RunResult:
        reads = ({"连通判定": "可达", "不可达原因": ""} if run.var == "d" else {})
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert res.finish_incomplete is False         # 整条非全空 → 合法答复


def test_finish_with_no_refs_is_not_incomplete():
    # A plain-text finish (no {var[field]}) can't be hollowed by an empty read → never flagged.
    prog = Program(statements=[
        Run(var="r", name="读", kind="read", returns=["x"], read_spec="y"),
        Finish(message="任务已完成"),   # 不引用任何 read
    ])
    res = ProgramRunner(lambda run: RunResult(completed=True, reads={"x": ""})).run(prog)
    assert res.finish_incomplete is False
