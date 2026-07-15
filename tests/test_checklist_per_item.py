"""Per-item checklist fold: each acceptance item gets its OWN status + evidence."""

from gui_agent.core.schemas import split_acceptance_items
from gui_agent.reports.statement_reducer import fold_checklist_from_checker

SUCCESS = "已进入评论页；筛选已应用且结果刷新；显示包含关键词的结果计数"


def _items(cmap: dict):
    return {i.text: i for i in cmap.values()}


def test_split_acceptance_items_matches_ordering():
    items = split_acceptance_items(SUCCESS)
    assert items == ["已进入评论页", "筛选已应用且结果刷新", "显示包含关键词的结果计数"]


def test_per_item_verdicts_set_independent_status_and_evidence():
    checker = {
        "status": "in_progress",
        "reason": "整体未完成",
        "visible_evidence": [],
        "missing_evidence": [],
        "item_verdicts": [
            {"index": 1, "met": True, "evidence": "顶部标题为 Reviews"},
            {"index": 2, "met": False, "evidence": "筛选框有文字但列表未刷新"},
            {"index": 3, "met": False, "evidence": ""},
        ],
    }
    cmap = fold_checklist_from_checker(
        success_condition=SUCCESS, fallback="m1", checker=checker,
    )
    items = _items(cmap)
    assert items["已进入评论页"].status == "done"
    assert items["已进入评论页"].evidence == ["顶部标题为 Reviews"]
    assert items["筛选已应用且结果刷新"].status == "pending"
    assert items["筛选已应用且结果刷新"].evidence == ["筛选框有文字但列表未刷新"]
    assert items["显示包含关键词的结果计数"].status == "pending"
    assert all(i.source == "checker:item_verdict" for i in cmap.values())


def test_done_status_is_sticky_across_turns():
    met = {"status": "in_progress", "reason": "", "item_verdicts": [{"index": 1, "met": True, "evidence": "ok"}]}
    cmap = fold_checklist_from_checker(
        success_condition="只有一项", fallback="m1", checker=met,
    )
    assert _items(cmap)["只有一项"].status == "done"
    unmet = {"status": "in_progress", "reason": "", "item_verdicts": [{"index": 1, "met": False, "evidence": "x"}]}
    fold_checklist_from_checker(
        success_condition="只有一项", fallback="m1", checker=unmet, items=cmap,
    )
    assert _items(cmap)["只有一项"].status == "done"


def test_missing_evidence_kept_at_true_status_not_forced_done():
    checker = {
        "status": "done",
        "reason": "整体完成",
        "visible_evidence": ["页面就位"],
        "missing_evidence": ["某遗留检查项未确认"],
        "item_verdicts": [{"index": 1, "met": True, "evidence": "ok"}],
    }
    cmap = fold_checklist_from_checker(
        success_condition="只有一项", fallback="m1", checker=checker,
    )
    items = _items(cmap)
    assert items["只有一项"].status == "done"
    assert items["某遗留检查项未确认"].status == "pending"
    assert items["某遗留检查项未确认"].source == "checker:missing_evidence"


def test_falls_back_to_shared_verdict_without_item_verdicts():
    checker = {
        "status": "done",
        "reason": "全部完成",
        "visible_evidence": ["ok"],
        "missing_evidence": [],
    }
    cmap = fold_checklist_from_checker(
        success_condition="一项A；一项B", fallback="m1", checker=checker,
    )
    items = _items(cmap)
    assert items["一项A"].status == "done"
    assert items["一项B"].status == "done"
