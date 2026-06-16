"""Per-item milestone checklist: each acceptance item gets its OWN status + evidence from the
checker's item_verdicts, rather than every item sharing the whole-milestone verdict."""

from gui_agent.core.run.state import _update_checklist_from_checker
from gui_agent.core.schemas import (
    MilestoneChecklistItem,
    MilestoneState,
    split_acceptance_items,
)

# success_condition with three sub-conditions (split on ；) → three checklist items.
SUCCESS = "已进入评论页；筛选已应用且结果刷新；显示包含关键词的结果计数"


def _items(state: MilestoneState) -> dict[str, MilestoneChecklistItem]:
    return {i.text: i for i in state.checklist}


def test_split_acceptance_items_matches_ordering():
    items = split_acceptance_items(SUCCESS)
    assert items == ["已进入评论页", "筛选已应用且结果刷新", "显示包含关键词的结果计数"]


def test_per_item_verdicts_set_independent_status_and_evidence():
    state = MilestoneState(id="m1")
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
    _update_checklist_from_checker(
        state, success_condition=SUCCESS, fallback="m1", checker=checker,
    )
    items = _items(state)
    assert items["已进入评论页"].status == "done"
    assert items["已进入评论页"].evidence == ["顶部标题为 Reviews"]
    assert items["筛选已应用且结果刷新"].status == "pending"
    assert items["筛选已应用且结果刷新"].evidence == ["筛选框有文字但列表未刷新"]
    # met=False with empty per-item evidence → falls back to the whole-verdict evidence (reason).
    assert items["显示包含关键词的结果计数"].status == "pending"
    assert all(i.source == "checker:item_verdict" for i in state.checklist)


def test_done_status_is_sticky_across_turns():
    state = MilestoneState(id="m1")
    met = {"status": "in_progress", "reason": "", "item_verdicts": [{"index": 1, "met": True, "evidence": "ok"}]}
    _update_checklist_from_checker(state, success_condition="只有一项", fallback="m1", checker=met)
    assert _items(state)["只有一项"].status == "done"
    # A later turn flickers to not-met; a done item must stay done (progress, not regress).
    unmet = {"status": "in_progress", "reason": "", "item_verdicts": [{"index": 1, "met": False, "evidence": "x"}]}
    _update_checklist_from_checker(state, success_condition="只有一项", fallback="m1", checker=unmet)
    assert _items(state)["只有一项"].status == "done"


def test_missing_evidence_kept_at_true_status_not_forced_done():
    state = MilestoneState(id="m1")
    checker = {
        "status": "done",
        "reason": "整体完成",
        "visible_evidence": ["页面就位"],
        "missing_evidence": ["某遗留检查项未确认"],
        "item_verdicts": [{"index": 1, "met": True, "evidence": "ok"}],
    }
    _update_checklist_from_checker(state, success_condition="只有一项", fallback="m1", checker=checker)
    items = _items(state)
    assert items["只有一项"].status == "done"
    # The milestone is done, but a missing_evidence row is NOT force-marked done — it keeps its
    # true status, so we never render a misleading "✓ 未选择文件".
    assert items["某遗留检查项未确认"].status == "pending"
    assert items["某遗留检查项未确认"].source == "checker:missing_evidence"


def test_falls_back_to_shared_verdict_without_item_verdicts():
    state = MilestoneState(id="m1")
    checker = {"status": "done", "reason": "全部满足", "visible_evidence": ["页面就位"], "missing_evidence": []}
    _update_checklist_from_checker(state, success_condition=SUCCESS, fallback="m1", checker=checker)
    items = _items(state)
    assert len(items) == 3
    assert all(i.status == "done" for i in state.checklist)
    assert all(i.source == "checker:success_condition" for i in state.checklist)
