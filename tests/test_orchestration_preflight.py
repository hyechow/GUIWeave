from inspect import signature

from gui_agent.core.orchestrator import Program, Query, Read, Run, validate_orchestration_preflight


def test_preflight_blocks_empty_program():
    result = validate_orchestration_preflight("Tell me the top search term", Program(statements=[]))

    assert not result.ok
    # empty/no-work hard-block; the answer-intent checks are warnings (fuzzy heuristic, non-blocking).
    assert [issue.code for issue in result.blocking_issues] == [
        "ORCH_EMPTY_PROGRAM",
        "ORCH_NO_EXECUTABLE_WORK",
    ]
    codes = [issue.code for issue in result.issues]
    assert "ORCH_ANSWER_WITHOUT_RESULT_SOURCE" in codes and "ORCH_ANSWER_WITHOUT_FINISH" in codes


def test_preflight_does_not_block_show_report_nav_task():
    # 707/708/709 regression: "Show/Create ... report" hits the answer-intent keywords, but these are
    # navigate-submit tasks (terminal action + dispatch gate, scored by NetworkEvent) with no
    # finish/returns. The answer-intent checks must be warnings, NOT block execution.
    for goal in (
        "Show the sales order report for last year",
        "Show the tax report for this year",
        "Create an orders report from May 1, 2021 to March 31, 2022",
    ):
        program = Program(goal=goal, statements=[
            Run(kind="navigation", name="进入 Reports > Sales"),
            Run(kind="filter", name="设置日期范围"),
            Run(kind="action", name="点击 Show Report"),
        ])
        result = validate_orchestration_preflight(goal, program)
        assert result.ok, f"{goal!r} should not be blocked: {[i.code for i in result.blocking_issues]}"


def test_preflight_accepts_mutation_without_finish_or_returns():
    program = Program(
        statements=[
            Run(kind="navigation", name="Open the product form"),
            Run(kind="action", name="Save the product"),
        ]
    )

    result = validate_orchestration_preflight("Update the product status to enabled", program)

    assert result.ok


def test_preflight_does_not_own_router_intent_contracts():
    assert "resolution" not in signature(validate_orchestration_preflight).parameters


# ── 执行模式纪律（交互/非交互边界；脚本生成视角的 lint）──────────────────────────────


def test_preflight_blocks_impure_precondition():
    """precondition = 纯 ensure-state 门：挂 returns 会读到错误帧（SC 被引擎重写为通用门）。"""
    program = Program(statements=[
        Run(kind="navigation", name="确保已登录", precondition=True, returns=["用户名"]),
        Run(kind="action", name="创建工单"),
    ])
    result = validate_orchestration_preflight("创建一个工单", program)
    assert any(i.code == "ORCH_PRECONDITION_IMPURE" for i in result.blocking_issues)


def test_preflight_accepts_pure_precondition():
    program = Program(statements=[
        Run(kind="navigation", name="确保已登录", precondition=True),
        Run(kind="action", name="创建工单"),
    ])
    result = validate_orchestration_preflight("创建一个工单", program)
    assert not any(i.code == "ORCH_PRECONDITION_IMPURE" for i in result.issues)


def test_preflight_warns_query_with_mutation_verb():
    """read/data_query 是纯查询原语，名字里的动作永远不会被执行——误分类要浮出来。"""
    program = Program(statements=[
        Read( name="点击导出按钮并读取总数", var="t", returns=["总数"]),
    ])
    result = validate_orchestration_preflight("统计总数", program)
    warning = [i for i in result.issues if i.code == "ORCH_QUERY_WITH_MUTATION_VERB"]
    assert warning and warning[0].severity == "warning"
    # 纯读取名不误报
    clean = Program(statements=[
        Read( name="读取当前页面显示的记录总数", var="t", returns=["总数"]),
    ])
    assert not any(
        i.code == "ORCH_QUERY_WITH_MUTATION_VERB"
        for i in validate_orchestration_preflight("统计总数", clean).issues
    )


def test_run_purity_vocabulary():
    assert Read( name="读").is_query
    assert Query( name="查", sql="SELECT 1").is_query
    for kind in ("navigation", "filter", "action"):
        run = Run(kind=kind, name="做")
        assert run.is_interactive and not run.is_query
