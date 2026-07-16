from gui_agent.core.orchestrator.intent_contracts import validate_intent_contracts
from gui_agent.core.orchestrator.program import Cond, ForEach, If, Program, Query, Run
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
        Run(
            kind="filter",
            var="f1",
            name="在 Bill-to Name 字段用精确值『Grace Nguyen』筛选",
            returns=["match_count"],
            read_spec="match_count：读取结果计数",
            target_values={"Bill-to Name": "Grace Nguyen"},
        ),
        If(
            cond=Cond(var="f1", field="match_count", cmp="==", value="0"),
            then=[Run(
                kind="filter",
                name="清除精确值后在同一 Bill-to Name 字段用关键词『Nguyen』重筛",
                target_values={"Bill-to Name": "Nguyen"},
            )],
        ),
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


def test_intent_contract_accepts_covers_set_aggregate_without_foreach():
    """Live trigger (WebArena 502): 'Mark all Gobi HeatTec Tee as out of stock' — router marks the
    entity as a set, but Magento's configurable PARENT save covers all variants at once. The
    covers_set declaration on the single mutation step satisfies the set contract without foreach."""
    program = Program(statements=[
        Run(kind="filter", name="在 Filters 面板的 Name 字段用精确值『Gobi HeatTec Tee』筛选，叠加 Type=Configurable Product"),
        Run(kind="navigation", name="打开 SKU=父SKU 的那一行编辑页"),
        Run(kind="action", name="将 Stock Status 下拉改为 Out of Stock 并保存",
            covers_set="Gobi HeatTec Tee"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Gobi HeatTec Tee",
            type="product",
            match_mode="approximate",
            search_key="HeatTec",
            cardinality="set",
            selector="Gobi HeatTec Tee",
        ),
    ])

    codes = _codes(program, resolution)
    assert "ROUTER_SET_ENTITY_WITHOUT_FOREACH" not in codes
    assert "ROUTER_SET_SELECTOR_NOT_APPLIED" not in codes


def test_intent_contract_covers_set_requires_entity_scope_in_retrieval():
    """covers_set alone is not enough: the retrieval steps must still scope to the entity,
    otherwise the aggregate mutation may act on the wrong group."""
    program = Program(statements=[
        Run(kind="filter", name="按 Type=Configurable Product 筛选"),
        Run(kind="action", name="将 Stock Status 改为 Out of Stock 并保存",
            covers_set="Gobi HeatTec Tee"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Gobi HeatTec Tee",
            type="product",
            match_mode="approximate",
            search_key="HeatTec",
            cardinality="set",
        ),
    ])

    assert "ROUTER_SET_ENTITY_WITHOUT_FOREACH" in _codes(program, resolution)


def test_intent_contract_key_dropped_rejects_downstream_token_substitution():
    program = Program(statements=[
        Run(kind="filter", name="在 Filters 面板的 Name 字段用精确值『Gobi HeatTec Tee』筛选"),
        Run(kind="filter", name="若 0 条，在同一 Name 字段用关键词『Gobi』重筛"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Gobi HeatTec Tee",
            type="product",
            match_mode="approximate",
            search_key="HeatTec",
        ),
    ])

    assert "ROUTER_APPROXIMATE_KEY_DROPPED" in _codes(program, resolution)


def test_later_navigation_mention_does_not_replace_exact_filter_trial():
    program = Program(statements=[
        Run(
            kind="filter",
            name="在 Name 字段用关键词『Nona』筛选",
            target_values={"Name": "Nona"},
        ),
        Run(kind="navigation", name="打开 Nona Fitness Tank 编辑页"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="Nona Fitness Tank",
            type="product",
            match_mode="approximate",
            search_key="Nona",
        ),
    ])

    assert "ROUTER_APPROXIMATE_MENTION_DROPPED" in _codes(program, resolution)


def test_intent_contract_key_dropped_ignores_generic_lowercase_tokens():
    """A generic lowercase mention token appearing in retrieval prose must not count as a
    fallback strategy — only name-like (uppercase-bearing or CJK) tokens qualify."""
    program = Program(statements=[
        Run(kind="filter", name="在 Orders grid 用精确值『pending orders report』筛选"),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="pending orders report",
            type="order",
            match_mode="approximate",
            search_key="pending",
        ),
    ])

    assert "ROUTER_APPROXIMATE_KEY_DROPPED" in _codes(program, resolution)


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


def test_intent_contract_accepts_consumed_value_role_entities():
    program = Program(statements=[
        Run(kind="navigation", name="进入 Cart Price Rules 页面"),
        Run(kind="action", name='填写 Rule Name 为 "Thanks giving sale"，Customer Groups 选择 all registered customers，保存'),
    ])
    resolution = IntentResolution(entities=[
        EntityRef(mention="Thanks giving sale", role="target_value", match_mode="approximate",
                  search_key="Thanksgiving"),
        EntityRef(mention="all registered customers", role="target_value", cardinality="set",
                  selector="registered"),
    ])

    assert validate_intent_contracts(program, resolution) == []


def test_intent_contract_blocks_value_only_present_in_goal_or_navigation():
    program = Program(
        goal="Add size XXXL to green product",
        statements=[
            Run(kind="navigation", name="进入 green product 编辑页"),
            Run(kind="action", name="添加 Size=XXXL 并保存"),
        ],
    )
    resolution = IntentResolution(entities=[
        EntityRef(mention="green", role="qualifier_value", match_mode="exact", search_key="green"),
        EntityRef(mention="XXXL", role="target_value", match_mode="exact", search_key="XXXL"),
    ])

    issues = validate_intent_contracts(program, resolution)

    assert {issue.code for issue in issues} == {"ROUTER_VALUE_DROPPED"}
    assert "green" in str(issues[0])


def test_intent_contract_requires_every_multi_value_member_to_be_consumed():
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="blue and purple",
            role="qualifier_value",
            value_members=["blue", "purple"],
            match_mode="exact",
        ),
    ])
    incomplete = Program(statements=[
        Run(
            kind="action",
            name="选择目标颜色并保存",
            target_values={"Color": "blue"},
        ),
    ])
    split = Program(statements=[
        Run(
            kind="action",
            name="选择第一个集合成员",
            target_values={"Color": "blue"},
        ),
        Run(
            kind="action",
            name="选择第二个集合成员",
            target_values={"Color": "purple"},
        ),
    ])
    complete = Program(statements=[
        Run(
            kind="action",
            name="保存完整颜色选择集",
            target_values={"Color": ["blue", "purple"]},
        ),
    ])

    assert "ROUTER_VALUE_DROPPED" in _codes(incomplete, resolution)
    assert "ROUTER_MULTI_VALUE_SPLIT" in _codes(split, resolution)
    assert validate_intent_contracts(complete, resolution) == []


def test_intent_contract_rejects_partial_actions_beside_complete_target_group():
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="30 and 31",
            role="target_value",
            value_members=["30", "31"],
            match_mode="exact",
        ),
    ])
    program = Program(statements=[
        Run(
            kind="action",
            name="单独添加 30",
            target_values={"Size": "30"},
        ),
        Run(
            kind="action",
            name="保存完整尺寸组合",
            target_values={"Size": ["30", "31"]},
        ),
    ])

    assert "ROUTER_MULTI_VALUE_SPLIT" in _codes(program, resolution)


def test_intent_contract_blocks_redefining_selection_only_values():
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="blue and purple",
            role="qualifier_value",
            value_members=["blue", "purple"],
        ),
        EntityRef(mention="XXS", role="target_value", search_key="XXS"),
    ])
    program = Program(statements=[
        Run(
            kind="action",
            name="创建 blue 和 purple 定义",
            target_values={"Label": ["blue", "purple"]},
        ),
        Run(
            kind="action",
            name="添加组合",
            target_values={"Color": ["blue", "purple"], "Size": "XXS"},
        ),
    ])

    assert "ROUTER_SELECTION_VALUE_REDEFINED" in _codes(program, resolution)


def test_intent_contract_blocks_selection_redefinition_hidden_in_action_text():
    resolution = IntentResolution(entities=[
        EntityRef(
            mention="blue and purple",
            role="qualifier_value",
            value_members=["blue", "purple"],
        ),
        EntityRef(mention="XXS", role="target_value", search_key="XXS"),
    ])
    program = Program(statements=[
        Run(kind="action", name="create blue and purple definitions"),
        Run(
            kind="action",
            name="create the requested variants",
            target_values={"Color": ["blue", "purple"], "Size": "XXS"},
        ),
    ])

    assert "ROUTER_SELECTION_VALUE_REDEFINED" in _codes(program, resolution)
