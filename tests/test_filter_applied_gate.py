"""Unit tests for the filter "action-applied" gate building blocks.

Covers the deterministic decoupling of "did the filter ACTION take effect" (the applied chips)
from "do the rendered rows look right" (the EFFECT) — the regression behind live run
20260629_173028, where the checker conflated Magento's `Salable Quantity` display column with
the filtered `Quantity` and rejected a correctly-applied `Quantity: 3 - 3` filter into a
clear→reset loop.
"""

from gui_agent.adapters.browser.filter_state import (
    applied_filters_js,
    normalize_applied_filter_state,
    normalize_applied_filters,
)
from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.helpers import (
    filter_chips_clean,
    filter_residual_labels,
    filter_state_satisfies_target,
    parse_filter_target,
)
from gui_agent.context.runtime import applied_filter_state_block


def _filter_ms(name: str, sc: str = "") -> Milestone:
    return Milestone(
        id="m_filter", name=name, description=name, success_condition=sc, kind="filter"
    )


# ── normalize_applied_filters ──────────────────────────────────────────────────
def test_normalize_parses_json_string_chips():
    raw = '{"Quantity": "3 - 3", "Store View": "Default Store View"}'
    assert normalize_applied_filters(raw) == {
        "Quantity": "3 - 3",
        "Store View": "Default Store View",
    }


def test_normalize_empty_or_garbage_is_none():
    assert normalize_applied_filters("{}") is None
    assert normalize_applied_filters("") is None
    assert normalize_applied_filters("not json") is None
    assert normalize_applied_filters(None) is None


def test_normalize_legacy_state_shape():
    raw = {
        "filters": {"Product": "Olivia"},
        "meta": {
            "source": "legacy_grid",
            "indicator_channel": "absent",
            "fallback_channel": "present",
            "chip_container": "absent",
            "legacy_grid": "present",
        },
    }
    filters, meta = normalize_applied_filter_state(raw)
    assert filters == {"Product": "Olivia"}
    assert meta["source"] == "legacy_grid"
    assert meta["indicator_channel"] == "absent"
    assert meta["fallback_channel"] == "present"
    assert normalize_applied_filters(raw) == {"Product": "Olivia"}


def test_applied_filters_js_targets_active_filter_chips():
    # Selector grounded against live Magento 2.4.6 DOM (probe_chips): ul.admin__current-filters-list
    # > li, label in span[data-bind*="label"], Remove button stripped.
    js = applied_filters_js()
    assert "admin__current-filters-list" in js
    assert 'data-bind*=\\"label\\"' in js or "data-bind*=" in js
    assert "button" in js  # the Remove button is stripped from the value
    assert "filter" in js and "legacy_grid" in js  # legacy Mage_Adminhtml grid fallback


# ── parse_filter_target ─────────────────────────────────────────────────────────
def test_parse_range_keeps_both_bounds():
    # task 185: name carries the structured range
    assert parse_filter_target(_filter_ms("清除无关筛选，设置 Quantity From=3 且 To=3")) == (
        "Quantity",
        ["3", "3"],
    )


def test_parse_range_distinct_bounds():
    assert parse_filter_target(_filter_ms("设置 Quantity From=2 To=3")) == (
        "Quantity",
        ["2", "3"],
    )


def test_parse_single_column_value():
    assert parse_filter_target(_filter_ms("应用 Status: Complete 筛选")) == (
        "Status",
        ["complete"],
    )


def test_parse_chinese_keyword_filter_target():
    assert parse_filter_target(_filter_ms("清除精确值筛选，在产品/Product列使用关键词'Olivia'进行筛选")) == (
        "Product",
        ["olivia"],
    )
    assert parse_filter_target(_filter_ms("可见筛选状态显示已应用 Product包含'Olivia'筛选")) == (
        "Product",
        ["olivia"],
    )


def test_parse_unparseable_is_none():
    assert parse_filter_target(_filter_ms("进入产品列表页并打开筛选面板")) is None


# ── filter_state_satisfies_target (the gate predicate) ──────────────────────────
def test_gate_fires_when_target_chip_present():
    ms = _filter_ms("清除无关筛选，设置 Quantity From=3 且 To=3",
                    "可见筛选状态显示 Quantity: 3 - 3")
    applied = {"Store View": "Default Store View", "Quantity": "3 - 3"}
    assert filter_state_satisfies_target(applied, ms) is True


def test_gate_does_not_fire_on_wrong_range():
    # The real failure's antidote: a 2-3 chip must NOT satisfy a 3-3 target.
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    assert filter_state_satisfies_target({"Quantity": "2 - 3"}, ms) is False


def test_gate_ignores_unrelated_display_column_values():
    # Salable Quantity present as a (hypothetical) chip must not be mistaken for Quantity.
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    applied = {"Salable Quantity": "2", "Quantity": "3 - 3"}
    assert filter_state_satisfies_target(applied, ms) is True


def test_gate_false_when_no_chips():
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    assert filter_state_satisfies_target(None, ms) is False
    assert filter_state_satisfies_target({}, ms) is False


def test_gate_false_when_target_unparseable():
    ms = _filter_ms("打开筛选面板")
    assert filter_state_satisfies_target({"Quantity": "3 - 3"}, ms) is False


# ── filter_chips_clean (residual-pollution guard, cf. task 186) ─────────────────
def test_chips_clean_target_plus_benign_store_view():
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    assert filter_chips_clean({"Store View": "Default Store View", "Quantity": "3 - 3"}, ms) is True


def test_chips_not_clean_with_leaked_residual():
    # A leaked Keyword filter (task 186 class) must block the gate so the clear duty is honored.
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    assert filter_chips_clean({"Keyword": "WS08", "Quantity": "3 - 3"}, ms) is False


# ── strong-path integration: the gate fires in the real policy.step(), no LLM ───
import gui_agent.core.supervisor.milestone.policy as policy_mod  # noqa: E402
from gui_agent.core.schemas import Observation  # noqa: E402

# A real (non-blank) PNG so is_loading_frame() doesn't short-circuit to a loading frame.
_FIXTURE_PNG = (
    "evals/browser/checker/screenshots/products_qty3_filter_salable_distractor.png"
)


def _qty3_filter_milestone() -> Milestone:
    return _filter_ms(
        "清除无关筛选，设置 Quantity From=3 且 To=3",
        "网格 Active filters 显示已生效筛选 Quantity: 3 - 3（控件状态达成即可，不需逐行复核库存）。",
    )


def _run_step(monkeypatch, applied_filters):
    """Drive a real MilestoneSupervisorPolicy.step() for the qty=3 filter milestone with the given
    applied_filters. Spies on run_checker: it must NOT be called when the gate fires (the gate is
    authoritative and skips the LLM checker), and MUST be called when it doesn't."""
    import os

    png = open(_FIXTURE_PNG, "rb").read() if os.path.exists(_FIXTURE_PNG) else b"\x89PNG\r\n\x1a\n"
    checker_calls: list[int] = []

    def _spy_run_checker(*a, **k):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    pol = policy_mod.MilestoneSupervisorPolicy()
    ms = _qty3_filter_milestone()
    pol.reseed(ms)
    obs = Observation(png_bytes=png, source="test", applied_filters=applied_filters)
    step = None
    try:
        step = pol.step(obs, goal="material of products with 3 units left", history=[])
    except _CheckerReached:
        pass
    return ms, step, checker_calls


class _CheckerReached(Exception):
    pass


def test_strong_gate_fires_done_without_invoking_checker(monkeypatch):
    ms, step, checker_calls = _run_step(monkeypatch, {"Quantity": "3 - 3"})
    assert checker_calls == [], "FilterGate must bypass the LLM checker when target chip is present"
    assert ms.status == "done"
    assert step is not None and step.goal_completed is True


def test_legacy_product_filter_gate_fires_without_invoking_checker(monkeypatch):
    ms = _filter_ms(
        "清除精确值筛选，在产品/Product列使用关键词'Olivia'进行筛选",
        "可见筛选状态显示已应用 Product包含'Olivia'筛选，列表已刷新且非0条记录",
    )
    import os

    png = open(_FIXTURE_PNG, "rb").read() if os.path.exists(_FIXTURE_PNG) else b"\x89PNG\r\n\x1a\n"
    checker_calls: list[int] = []

    def _spy_run_checker(*a, **k):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    pol = policy_mod.MilestoneSupervisorPolicy()
    pol.reseed(ms)
    obs = Observation(
        png_bytes=png,
        source="test",
        applied_filters={"Product": "Olivia"},
        applied_filter_meta={"source": "legacy_grid", "indicator_channel": "absent", "fallback_channel": "present"},
    )
    step = pol.step(obs, goal="reviews for Olivia zip jacket", history=[])
    assert checker_calls == []
    assert ms.status == "done"
    assert step.goal_completed is True


def test_chip_absent_channel_block_warns_not_to_wait_for_chips():
    block = applied_filter_state_block(
        None,
        {"source": "none", "indicator_channel": "absent", "fallback_channel": "present"},
    )
    assert block is not None
    text = block.render()
    assert "缺少某种常见的筛选状态指示通道" in text
    assert "不能把" in text
    assert "重复提交同一动作" in text


def test_no_chips_falls_through_to_checker(monkeypatch):
    # No applied_filters signal from any adapter mechanism → gate cannot fire → checker runs.
    _ms, _step, checker_calls = _run_step(monkeypatch, None)
    assert checker_calls == [1], "without applied_filters the gate must not fire; checker runs"


def test_wrong_range_chip_falls_through_to_checker(monkeypatch):
    # A 2-3 chip does not satisfy a 3-3 target → gate must not fire → checker runs.
    _ms, _step, checker_calls = _run_step(monkeypatch, {"Quantity": "2 - 3"})
    assert checker_calls == [1]


# ── filter_residual_labels (runtime state-diff: clear only unrelated residuals) ──
def test_residuals_only_the_leaked_chip_not_the_target():
    # The 186 scenario done right: a leaked `Keyword: WS08` is residual; the task's own Quantity
    # filter and the benign Store View are KEPT — no blanket Clear-all.
    ms = _filter_ms("清除残留筛选，设置 Quantity From=3 且 To=3")
    applied = {"Quantity": "3 - 3", "Keyword": "WS08", "Store View": "Default Store View"}
    assert filter_residual_labels(applied, ms) == ["Keyword"]


def test_no_residuals_when_only_target_and_benign():
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    assert filter_residual_labels({"Quantity": "3 - 3", "Store View": "x"}, ms) == []


def test_no_filter_intent_makes_every_chip_residual():
    # "any state / 全量" task: the intent is NO filter, so every non-benign chip is residual.
    ms = _filter_ms("清除筛选，准备全量 all orders 数据源（不限状态）")
    assert filter_residual_labels({"Status": "Complete", "Store View": "x"}, ms) == ["Status"]


def test_unparseable_non_nofilter_target_yields_no_residuals():
    # Can't diff without an intent → return [] (don't guess / don't blanket-clear).
    ms = _filter_ms("打开筛选面板")
    assert filter_residual_labels({"Keyword": "WS08"}, ms) == []


def test_residuals_empty_when_no_applied_filters():
    ms = _filter_ms("设置 Quantity From=3 且 To=3")
    assert filter_residual_labels(None, ms) == []


def test_keyword_search_treats_leftover_column_filter_as_residual():
    # live 114706: searching WS08 via Search by keyword while Quantity:3-3 is still applied →
    # keyword+column AND → the qty=3 child, not the qty=0 Configurable parent. The leftover
    # Quantity column filter IS a residual for a keyword-search milestone; the Keyword chip is kept.
    ms = _filter_ms("回到 Products 列表，用顶部 Search by keyword 框搜父产品 SKU WS08")
    assert filter_residual_labels({"Quantity": "3 - 3"}, ms) == ["Quantity"]
    assert filter_residual_labels(
        {"Keyword": "WS08", "Quantity": "3 - 3", "Store View": "x"}, ms
    ) == ["Quantity"]


def test_quantity_filter_milestone_not_treated_as_keyword_search():
    # a real Quantity column-filter milestone must NOT be mis-read as keyword-search.
    ms = _filter_ms("清除残留筛选，设置 Quantity From=3 且 To=3")
    assert filter_residual_labels({"Quantity": "3 - 3", "Keyword": "WS08"}, ms) == ["Keyword"]
