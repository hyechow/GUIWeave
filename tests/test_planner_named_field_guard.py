from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.helpers import (
    _guard_named_field_substitution_plan,
    _guard_stale_text_filter_plan,
)
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def _check() -> _SingleCheckResult:
    return _SingleCheckResult(status="in_progress", reason="尚未筛选", summary="等待操作")


def test_named_field_guard_does_not_substitute_visible_other_field():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "在产品列使用关键词'Olivia'进行模糊搜索筛选",
        "description": "在产品列使用关键词'Olivia'进行模糊搜索筛选",
        "success_condition": "产品列已按 Olivia 筛选",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "nickname", "kind": "text_input"},
            {"label": "detail", "kind": "text_input"},
            {"label": "sku", "kind": "text_input"},
        ],
    )
    plan = _PlanResult(instruction="在 'Nickname' 输入框输入 Olivia", summary="误把目标字段换成可见字段")

    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)

    assert "产品" in guarded.instruction
    assert "Nickname" not in guarded.instruction
    assert guarded.direction == "right"


def test_named_field_guard_treats_chinese_product_column_as_product_control():
    """Regression 20260707_115911: "在产品列" was extracted as the literal field "在产品",
    so the guard rewrote a correct Product-input plan into a horizontal-scroll hunt."""
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "在产品列用精确值「Olivia zip jacket」筛选",
        "description": "在产品列用精确值「Olivia zip jacket」筛选",
        "success_condition": "已应用 Product 精确筛选",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Product", "kind": "text_input", "value": "Olivia"},
        ],
    )
    plan = _PlanResult(
        instruction="在 Product 输入框填入 'Olivia zip jacket'",
        summary="当前 Product 筛选器仅包含 Olivia，需要更新为完整目标值",
    )

    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == plan.instruction
    assert guarded.direction is None


def test_named_field_guard_retargets_when_target_control_is_visible():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "在 Product 列使用关键词 Olivia 进行筛选",
        "description": "在 Product 列使用关键词 Olivia 进行筛选",
        "success_condition": "Product 列已按 Olivia 筛选",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Nickname", "kind": "text_input"},
            {"label": "Product", "kind": "text_input"},
        ],
    )
    plan = _PlanResult(instruction="在 Nickname 输入框输入 Olivia", summary="误指向 Nickname")

    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == "在 Product 输入框输入 Olivia"
    assert guarded.direction is None


def test_stale_filter_guard_blocks_submitting_old_dom_value():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "清除当前筛选，在 Product 列改用关键词 'Olivia' 重新提交筛选",
        "description": "当前精确词 'Olivia zip jacket' 已提交但返回 0 records found；本步必须把 Product 列筛选词改成关键词 'Olivia'。",
        "success_condition": "Product 列筛选框 current='Olivia'，且提交后列表显示非 0 条结果",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Product", "kind": "text_input", "value": "Olivia zip jacket"},
        ],
    )
    plan = _PlanResult(instruction="点击 'Search' 按钮提交筛选操作", summary="提交旧筛选")

    guarded = _guard_stale_text_filter_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == "在 Product 输入框输入 Olivia"
    assert "不能提交旧值" in guarded.summary


def test_stale_filter_guard_allows_explicit_reset_before_retyping():
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "清除当前筛选，在 Product 列改用关键词 'Olivia' 重新提交筛选",
        "description": "本步必须把 Product 列筛选词改成关键词 'Olivia'。",
        "success_condition": "Product 列筛选框 current='Olivia'",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Product", "kind": "text_input", "value": "Olivia zip jacket"},
        ],
    )
    plan = _PlanResult(instruction="点击 'Reset Filter' 按钮清除当前筛选", summary="先清除")

    guarded = _guard_stale_text_filter_plan(plan, milestone, _check(), observation)

    assert guarded.instruction == plan.instruction


def test_list_page_name_not_extracted_as_filter_column():
    """`Products 列表`(the Products LIST page) must NOT be parsed as a "Products" column — else the
    named-field guard hijacks a keyword search into a column-filter hunt (live 102944: the agent
    scrolled right hunting a「Products」column filter instead of using Search by keyword)."""
    from gui_agent.core.supervisor.milestone.helpers import _extract_target_fields

    m = Milestone.model_validate({
        "id": "d", "kind": "filter",
        "name": "回到 Catalog > Products 列表页，在顶部 Search by keyword 输入框输入父产品 SKU WS08 并提交搜索",
        "description": "", "success_condition": "",
    })
    fields = _extract_target_fields(m)
    assert "Products" not in fields, fields
    # a genuine column-filter milestone still extracts its column
    m2 = Milestone.model_validate({
        "id": "x", "kind": "filter", "name": "在 Status 列筛选 Complete",
        "description": "", "success_condition": "",
    })
    assert "Status" in _extract_target_fields(m2)


def test_keyword_search_on_list_page_not_hijacked_to_column_filter():
    """BAD CASE 102944: a function milestone 'go to the Products LIST, type SKU into Search by
    keyword, submit' is a GLOBAL keyword search — the named-field guard must NOT mistake the
    'Products 列表' (list page) for a 'Products' column and rewrite the plan into a horizontal-scroll
    hunt for a column filter. The agent thrashed 25 turns scrolling right for a「Products」filter
    box instead of using the Search by keyword box it was already pointed at."""
    milestone = Milestone.model_validate({
        "id": "d",
        "name": "回到 Catalog > Products 列表页，在顶部 Search by keyword 输入框输入父产品 SKU WS08，按回车提交搜索",
        "description": "",
        "success_condition": "Products 列表已按关键词 WS08 刷新，出现 SKU=WS08、Type=Configurable 的行",
        "kind": "filter",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {"label": "Search by keyword", "kind": "text_input"},
        ],
    )
    plan = _PlanResult(
        instruction="在 Search by keyword 输入框输入 WS08 并提交搜索",
        summary="用关键词搜索框搜父产品 SKU",
    )

    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)

    # must NOT hijack into a column-filter hunt
    assert "横向滚动" not in (guarded.instruction or "")
    assert guarded.direction != "right"
    assert "Search by keyword" in (guarded.instruction or "")


def test_runtime_retry_annotation_not_extracted_as_field():
    """The orchestrator's empty-returns retry annotation `（继续定位返回字段：material）` must NOT be
    parsed as a「继续定位返回」column (live 120601: it hijacked a read into a column-filter scroll)."""
    from gui_agent.core.supervisor.milestone.helpers import _extract_target_fields

    m = Milestone.model_validate({
        "id": "d", "kind": "navigation",
        "name": "点搜索结果里 SKU=WS08 那一行的 Edit 链接，打开它的编辑页（继续定位返回字段：material）",
        "description": "", "success_condition": "",
    })
    assert _extract_target_fields(m) == []


def test_named_field_guard_skipped_for_navigation_read_milestones():
    """A navigation/read milestone targets a control by role (open Edit, read a value), not a named
    grid column — the column-substitution guard must not touch it."""
    milestone = Milestone.model_validate({
        "id": "d", "kind": "navigation",
        "name": "点 SKU=WS08 行的 Edit 链接打开编辑页，读取 Material（继续定位返回字段：material）",
        "description": "", "success_condition": "进入编辑页",
    })
    observation = Observation(
        png_bytes=b"png", source="browser",
        form_controls=[{"label": "Material", "kind": "native_select"}],
    )
    plan = _PlanResult(instruction="向下滚动以显示 Material 字段并读取", summary="读 material")
    guarded = _guard_named_field_substitution_plan(plan, milestone, _check(), observation)
    assert guarded.instruction == plan.instruction  # untouched
    assert "横向滚动" not in (guarded.instruction or "")
