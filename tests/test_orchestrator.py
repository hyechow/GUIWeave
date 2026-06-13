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


def test_task_type_for_read_is_analysis():
    from gui_agent.core.orchestrator.engine import task_type_for
    assert task_type_for(Run(name="读", kind="read", returns=["x"])) == "analysis"
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
    # 先塞点 per-milestone 脏状态，验证 reseed 会清
    p._milestones = {"old": object()}  # type: ignore[dict-item]
    p._order = ["old", "x"]
    p._scroll_counts = {"old": 5}
    run = Run(var="d", name="读判定", kind="read", returns=["连通判定"])
    p.reseed(to_milestone(run, 0), task_type=task_type_for(run))
    assert list(p._order) == ["d"] and p._current_id == "d"
    assert p.task_type == "analysis"          # read → analysis（读取门）
    assert p._scroll_counts == {}             # per-milestone 追踪已清


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
