from gui_agent.core.orchestrator import Finish, ForEach, Program, Read, Query, Run, validate_orchestration_preflight
from gui_agent.core.router import EntityRef, IntentResolution


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


def test_preflight_blocks_approximate_entity_when_search_key_is_missing():
    program = Program(
        statements=[
            Run(kind="filter", name="Search product Olivia zip jacket in Reviews grid"),
            Read( var="r", name="Read matching review", returns=["nickname"]),
            Finish(message="{r[nickname]}"),
        ]
    )
    resolution = IntentResolution(
        entities=[
            EntityRef(
                mention="Olivia zip jacket",
                type="product",
                match_mode="approximate",
                search_key="Olivia",
            )
        ]
    )

    result = validate_orchestration_preflight("Tell me the nickname for Olivia zip jacket", program, resolution=resolution)

    assert not result.ok
    assert any(issue.code == "ROUTER_APPROXIMATE_KEY_DROPPED" for issue in result.blocking_issues)


def test_preflight_accepts_approximate_entity_when_search_key_is_preserved():
    program = Program(
        statements=[
            Run(kind="filter", name="Search Product column by Olivia zip jacket"),
            Run(kind="filter", name="If no match, search Product column by Olivia"),
            Read( var="r", name="Read matching review", returns=["nickname"]),
            Finish(message="{r[nickname]}"),
        ]
    )
    resolution = IntentResolution(
        entities=[
            EntityRef(
                mention="Olivia zip jacket",
                type="product",
                match_mode="approximate",
                search_key="Olivia",
            )
        ]
    )

    result = validate_orchestration_preflight("Tell me the nickname for Olivia zip jacket", program, resolution=resolution)

    assert result.ok


def test_preflight_blocks_set_entity_without_foreach():
    program = Program(
        statements=[
            Run(kind="filter", name="Filter products by size 28"),
            Run(kind="action", name="Update the first matching product"),
            Finish(message="done"),
        ]
    )
    resolution = IntentResolution(
        entities=[
            EntityRef(
                mention="size 28 products",
                type="product",
                match_mode="approximate",
                search_key="size 28",
                cardinality="set",
                selector="size 28",
            )
        ]
    )

    result = validate_orchestration_preflight("Update all size 28 products", program, resolution=resolution)

    assert not result.ok
    assert any(issue.code == "ROUTER_SET_ENTITY_WITHOUT_FOREACH" for issue in result.blocking_issues)


def test_preflight_accepts_mutation_without_finish_or_returns():
    program = Program(
        statements=[
            Run(kind="navigation", name="Open the product form"),
            Run(kind="action", name="Save the product"),
        ]
    )

    result = validate_orchestration_preflight("Update the product status to enabled", program)

    assert result.ok


def test_preflight_accepts_set_entity_with_foreach():
    # Contract update (live 778 run 235723): membership riding only in the collect TARGET text is
    # NOT filtering — the collector greps grid rows and ignores the description. The foreach must
    # carry a real mechanism; here: member_desc (selection checkpoint).
    program = Program(
        statements=[
            ForEach(
                var="row",
                target="products matching size 28",
                member_desc="size 28 的产品",
                returns=["sku"],
                body=[Run(kind="action", name="Update product {row[sku]}")],
            ),
            Finish(message="done"),
        ]
    )
    resolution = IntentResolution(
        entities=[
            EntityRef(
                mention="size 28 products",
                type="product",
                match_mode="approximate",
                search_key="size 28",
                cardinality="set",
                selector="size 28",
            )
        ]
    )

    result = validate_orchestration_preflight("Update all size 28 products", program, resolution=resolution)

    assert result.ok


def test_preflight_skips_value_role_entities():
    # 702/703 regression: values to SET (a new rule's name, a form scope) are used verbatim, never
    # searched/iterated — coverage checks on them false-blocked live-green tasks.
    program = Program(statements=[
        Run(kind="navigation", name="进入 Cart Price Rules 页面"),
        Run(kind="action", name='填写 Rule Name 为 "Thanks giving sale"，Customer Groups 全选，保存'),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(mention="Thanks giving sale", role="value", match_mode="approximate",
                  search_key="Thanksgiving"),           # even with a normalized key → skipped
        EntityRef(mention="all registered customers", role="value", cardinality="set",
                  selector="registered"),               # scope setting marked set → skipped
    ])

    result = validate_orchestration_preflight(
        'Create a new marketing price rule called "Thanks giving sale"', program, resolution=resolution)

    assert result.ok, [i.code for i in result.blocking_issues]


def test_preflight_blocks_set_with_foreach_but_no_membership():
    # Live 778 run 235723: foreach over ALL 7 Sahara rows (into named "size28_leggings" — naming is
    # not filtering) straight into a price-cut call — would mutate -29- variants and the parent.
    program = Program(statements=[
        Run(kind="filter", name="搜索 Sahara"),
        ForEach(var="legging", target="Sahara 行", returns=["id", "detail_url", "sku"],
                body=[Run(kind="action", name="打开 {legging[detail_url]} 并降价保存")]),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(mention="size 28 Sahara leggings", match_mode="approximate",
                  search_key="Sahara", cardinality="set", selector="size 28"),
    ])
    result = validate_orchestration_preflight("Reduce the price of size 28 Sahara leggings", program,
                                              resolution=resolution)
    assert any(i.code == "ROUTER_SET_SELECTOR_NOT_APPLIED" for i in result.blocking_issues)


def test_preflight_accepts_membership_via_member_desc_or_bodygoal():
    resolution = IntentResolution(entities=[
        EntityRef(mention="size 28 Sahara leggings", match_mode="approximate",
                  search_key="Sahara", cardinality="set", selector="size 28"),
    ])
    with_md = Program(statements=[
        ForEach(var="row", target="Sahara 行", returns=["sku"], member_desc="size 28 的变体",
                body=[Run(kind="action", name="处理 {row[sku]}")]),
    ])
    assert validate_orchestration_preflight("Reduce ...", with_md, resolution=resolution).ok
    with_bg = Program(statements=[
        ForEach(var="row", target="Sahara 行", returns=["sku"],
                body_goal="判断 {row[sku]} 是否 size 28;若是降价保存"),
    ])
    assert validate_orchestration_preflight("Reduce ...", with_bg, resolution=resolution).ok


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
