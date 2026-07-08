from gui_agent.core.orchestrator.intent_contracts import validate_intent_contracts
from gui_agent.core.orchestrator.program import ForEach, Program, Query, Run
from gui_agent.core.router import EntityRef, IntentResolution


def _codes(program: Program, resolution: IntentResolution) -> set[str]:
    return {issue.code for issue in validate_intent_contracts(program, resolution)}


def _issues(program: Program, resolution: IntentResolution):
    return validate_intent_contracts(program, resolution)


def test_intent_contract_blocks_approximate_key_drop():
    program = Program(statements=[
        Run(kind="filter", name="Search product Olivia zip jacket in Reviews grid"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Olivia zip jacket",
            type="product",
            match_mode="approximate",
            search_key="Olivia",
        ),
    ])

    assert "ROUTER_APPROXIMATE_KEY_DROPPED" in _codes(program, resolution)


def test_intent_contract_blocks_approximate_mention_drop():
    program = Program(statements=[
        Run(kind="filter", name="在 Bill-to Name 字段用关键词 Nguyen 筛选客户"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Grace Nguyen",
            type="customer",
            match_mode="approximate",
            search_key="Nguyen",
        ),
    ])

    assert "ROUTER_APPROXIMATE_MENTION_DROPPED" in _codes(program, resolution)


def test_approximate_mention_feedback_names_exact_and_fallback_values():
    program = Program(statements=[
        Run(kind="filter", name="在顶部搜索框输入客户名『Grace』并提交搜索"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Grace Nguyen",
            type="customer",
            match_mode="approximate",
            search_key="Grace",
        ),
    ])

    issue = next(i for i in _issues(program, resolution) if i.code == "ROUTER_APPROXIMATE_MENTION_DROPPED")

    assert "Grace Nguyen" in str(issue)
    assert "Grace" in str(issue)
    assert "K-only" in str(issue)
    assert "count == '0'" in str(issue)


def test_intent_contract_does_not_count_query_name_as_approximate_retrieval():
    program = Program(statements=[
        Run(kind="filter", name="在 Bill-to Name 字段用关键词 Grace 筛选客户"),
        ForEach(var="row", into="orders", row_fields=["bill_to_name", "status", "purchase_date", "action_url"]),
        Query(
            var="q",
            name="选出 Grace Nguyen 最近一笔 pending 订单",
            returns=["action_url"],
            sql="SELECT action_url FROM orders WHERE bill_to_name LIKE '%Grace%' ORDER BY purchase_date_ts DESC LIMIT 1",
        ),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Grace Nguyen",
            type="customer",
            match_mode="approximate",
            search_key="Grace",
        ),
    ])

    assert "ROUTER_APPROXIMATE_MENTION_DROPPED" in _codes(program, resolution)


def test_intent_contract_accepts_approximate_exact_then_fallback():
    program = Program(statements=[
        Run(kind="filter", name="在 Bill-to Name 字段用精确值『Grace Nguyen』筛选"),
        Run(kind="filter", name="若 0 条，在同一 Bill-to Name 字段用关键词『Nguyen』重筛"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Grace Nguyen",
            type="customer",
            match_mode="approximate",
            search_key="Nguyen",
        ),
    ])

    codes = _codes(program, resolution)
    assert "ROUTER_APPROXIMATE_MENTION_DROPPED" not in codes
    assert "ROUTER_APPROXIMATE_KEY_DROPPED" not in codes


def test_intent_contract_accepts_set_entity_split_into_key_and_selector():
    program = Program(statements=[
        Run(kind="filter", name="搜索 Sahara 候选记录"),
        ForEach(
            var="row",
            target="Sahara 行",
            row_fields=["sku"],
            member_desc="size 28 的变体",
            body=[Run(kind="action", name="处理 {row[sku]}")],
        ),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="size 28 Sahara leggings",
            type="product",
            match_mode="approximate",
            search_key="Sahara",
            cardinality="set",
            selector="size 28",
        ),
    ])

    assert "ROUTER_APPROXIMATE_MENTION_DROPPED" not in _codes(program, resolution)


def test_intent_contract_requires_foreach_for_set_entity():
    program = Program(statements=[
        Run(kind="filter", name="Filter products by size 28"),
        Run(kind="action", name="Update the first matching product"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="size 28 products",
            type="product",
            match_mode="approximate",
            search_key="size 28",
            cardinality="set",
            selector="size 28",
        ),
    ])

    assert "ROUTER_SET_ENTITY_WITHOUT_FOREACH" in _codes(program, resolution)


def test_intent_contract_requires_membership_mechanism_for_set_selector():
    program = Program(statements=[
        Run(kind="filter", name="搜索 Sahara"),
        ForEach(
            var="row",
            into="variant_rows",
            row_fields=["sku", "name", "action_url"],
            body=[Run(kind="action", name="打开 {row[action_url]} 并降价保存")],
        ),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="size 28 Sahara leggings",
            match_mode="approximate",
            search_key="Sahara",
            cardinality="set",
            selector="size 28",
        ),
    ])

    assert "ROUTER_SET_SELECTOR_NOT_APPLIED" in _codes(program, resolution)


def test_intent_contract_accepts_member_desc_for_set_selector():
    program = Program(statements=[
        Run(kind="filter", name="搜索 Sahara"),
        ForEach(
            var="row",
            into="variant_rows",
            row_fields=["sku", "name", "action_url"],
            member_desc="size 28 的 Sahara leggings 变体",
            body=[Run(kind="action", name="打开 {row[action_url]} 并降价保存")],
        ),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="size 28 Sahara leggings",
            match_mode="approximate",
            search_key="Sahara",
            cardinality="set",
            selector="size 28",
        ),
    ])

    assert "ROUTER_SET_SELECTOR_NOT_APPLIED" not in _codes(program, resolution)


def test_intent_contract_blocks_missing_entity_scope_predicate_on_foreach_query():
    program = Program(statements=[
        Run(name="按产品 Olivia 过滤评论列表", kind="filter",
            success_condition="Active filters 显示 Product: Olivia"),
        ForEach(var="row", into="reviews", returns=["nickname", "rating"]),
        Query(
            var="q",
            name="筛低分昵称",
            returns=["nickname"],
            sql="SELECT nickname FROM reviews WHERE CAST(rating AS INTEGER) <= 3",
        ),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(mention="Olivia zip jacket", match_mode="approximate", search_key="Olivia"),
    ])

    assert "ENTITY_SCOPE_PREDICATE_MISSING" in _codes(program, resolution)


def test_intent_contract_accepts_real_entity_scope_predicate_on_foreach_query():
    program = Program(statements=[
        Run(name="按产品 Erica 过滤评论列表", kind="filter",
            success_condition="Active filters 显示 Product: Erica"),
        ForEach(var="row", into="review_rows", row_fields=["Product", "Title", "Action_url", "rating"]),
        Query(
            var="q",
            name="筛低分评论",
            returns=["result"],
            sql=(
                "SELECT summary_of_review AS title, rating FROM review_rows "
                "WHERE product LIKE '%Erica%' AND CAST(rating AS INTEGER) <= 3"
            ),
        ),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(mention="Erica Sports Bra", type="product", match_mode="approximate", search_key="Erica"),
    ])

    assert "ENTITY_SCOPE_PREDICATE_MISSING" not in _codes(program, resolution)


def test_intent_contract_skips_value_role_entities():
    program = Program(statements=[
        Run(kind="navigation", name="进入 Cart Price Rules 页面"),
        Run(kind="action", name='填写 Rule Name 为 "Thanks giving sale"，Customer Groups 全选，保存'),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(mention="Thanks giving sale", role="value", match_mode="approximate",
                  search_key="Thanksgiving"),
        EntityRef(mention="all registered customers", role="value", cardinality="set",
                  selector="registered"),
    ])

    assert validate_intent_contracts(program, resolution) == []
