from gui_agent.core.orchestrator import Finish, ForEach, Program, Run, validate_orchestration_preflight
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
            Run(kind="read", var="r", name="Read matching review", returns=["nickname"]),
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
            Run(kind="filter", name="Search Product column by Olivia"),
            Run(kind="read", var="r", name="Read matching review", returns=["nickname"]),
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
    program = Program(
        statements=[
            ForEach(
                var="row",
                target="products matching size 28",
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
