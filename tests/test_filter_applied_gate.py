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
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Milestone,
    Observation,
    SupervisorStep,
)
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.supervisor.milestone.evidence import runtime_filter_intent
from gui_agent.core.supervisor.milestone.observation_state import (
    filter_chips_clean,
    filter_residual_labels,
    filter_state_satisfies_target,
    observed_filter_intent,
    RuntimeFilterIntent,
)
from gui_agent.context.runtime import applied_filter_state_block


def _filter_ms(
    name: str,
    sc: str = "",
    *,
    target_values: dict[str, str] | None = None,
) -> Milestone:
    return Milestone(
        id="m_filter",
        name=name,
        description=name,
        success_condition=sc,
        kind="filter",
        target_values=target_values or {},
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


def test_runtime_write_intent_matches_actual_keyword_route_without_text_parser():
    milestone = _filter_ms(
        "在产品名称字段用精确值『Minerva LumaTech V-Tee』筛选",
        "产品名称精确筛选已应用，records-found 计数可读",
    )
    intent = RuntimeFilterIntent(
        target_control="Search by keyword",
        target_value="Minerva LumaTech V-Tee",
    )

    assert filter_state_satisfies_target(
        {"Keyword": "Minerva LumaTech V-Tee"}, milestone, intent
    ) is True
    assert filter_chips_clean(
        {"Keyword": "Minerva LumaTech V-Tee"}, milestone, intent
    ) is True


def test_runtime_write_refines_semantic_filter_control_for_same_declared_value():
    milestone = _filter_ms(
        "在 Name 列用关键词『Minerva』筛选",
        target_values={"Name": "Minerva"},
    )
    intent = RuntimeFilterIntent(
        target_control="Search by keyword",
        target_value="Minerva",
    )
    applied = {"Keyword": "Minerva"}

    assert filter_state_satisfies_target(applied, milestone, intent) is True
    assert filter_chips_clean(applied, milestone, intent) is True
    assert filter_residual_labels(applied, milestone, intent) == []


def test_observed_filter_state_binds_prepopulated_concrete_control():
    milestone = _filter_ms(
        "在 Name 列用关键词『Minerva』筛选",
        target_values={"Name": "Minerva"},
    )
    intent = observed_filter_intent(
        {"Keyword": "Minerva"},
        [{
            "label": "Search by keyword",
            "kind": "text_input",
            "value": "Minerva",
        }],
        milestone,
    )

    assert intent == RuntimeFilterIntent("Search by keyword", "Minerva")


def test_observed_filter_state_refines_unstructured_runtime_control() -> None:
    milestone = _filter_ms(
        "清除精确值后在同一产品名称字段用关键词 'Minerva' 重筛",
        "产品名称关键词筛选已应用且匹配记录非 0 条",
    )
    intent = observed_filter_intent(
        {"Keyword": "Minerva"},
        [{
            "label": "Search by keyword",
            "kind": "text_input",
            "value": "Minerva",
        }],
        milestone,
        RuntimeFilterIntent("Name", "Minerva"),
    )

    assert intent == RuntimeFilterIntent("Search by keyword", "Minerva")


def test_observed_filter_state_does_not_override_runtime_value() -> None:
    milestone = _filter_ms("筛选目标记录")
    intent = observed_filter_intent(
        {"Keyword": "LumaTech"},
        [{
            "label": "Search by keyword",
            "kind": "text_input",
            "value": "LumaTech",
        }],
        milestone,
        RuntimeFilterIntent("Name", "Minerva"),
    )

    assert intent is None


def test_observed_filter_state_rejects_ambiguous_concrete_controls():
    milestone = _filter_ms(
        "筛选目标值",
        target_values={"Name": "same"},
    )
    intent = observed_filter_intent(
        {"Keyword": "same"},
        [
            {"label": "Search by keyword", "value": "same"},
            {"label": "Keyword search", "value": "same"},
        ],
        milestone,
    )

    assert intent is None


def test_commit_receipt_carries_filter_intent_when_control_was_prepopulated():
    milestone = _filter_ms(
        "在 Name 列用关键词『Minerva』筛选",
        target_values={"Name": "Minerva"},
    )
    scope = f"milestone:{milestone.id}"
    step = SupervisorStep(
        should_act=True,
        instruction="按回车键提交",
        stop=False,
        goal_completed=False,
        summary="submit populated filter",
        milestone_id=milestone.id,
        execution_scope=scope,
        milestone_kind="filter",
        atomic_role="commit",
        action_family="input",
        target_control="Search by keyword",
        target_value="Minerva",
    )
    turn = make_interactive_turn(
        index=1,
        observation_source="browser",
        supervisor_step=step,
        action_decision=BaseActionDecision(action=BaseAction(
            action_type="press_enter",
            description="submit populated filter",
        )),
        executed=True,
    )

    assert runtime_filter_intent(
        milestone, [turn], scope=scope
    ) == RuntimeFilterIntent("Search by keyword", "Minerva")


def test_runtime_write_cannot_refine_ambiguous_equal_declared_values():
    milestone = _filter_ms(
        "设置两个独立筛选字段",
        target_values={"Primary": "same", "Secondary": "same"},
    )
    intent = RuntimeFilterIntent(
        target_control="Broad search",
        target_value="same",
    )

    assert filter_state_satisfies_target(
        {"Search": "same"}, milestone, intent
    ) is False


def test_runtime_write_cannot_override_declared_filter_value():
    milestone = _filter_ms(
        "在 Name 列筛选『Minerva』",
        target_values={"Name": "Minerva"},
    )
    intent = RuntimeFilterIntent(
        target_control="Search by keyword",
        target_value="LumaTech",
    )

    assert filter_state_satisfies_target(
        {"Keyword": "LumaTech"}, milestone, intent
    ) is False
    assert filter_residual_labels(
        {"Keyword": "LumaTech"}, milestone, intent
    ) == ["Keyword"]


def test_runtime_write_intent_zero_result_completes_before_checker(monkeypatch):
    milestone = _filter_ms(
        "在产品名称字段用精确值『Minerva LumaTech V-Tee』筛选",
        "产品名称精确筛选已应用，records-found 计数可读",
        target_values={"Name": "Minerva LumaTech V-Tee"},
    )
    milestone.returns = ["match_count"]
    write_step = SupervisorStep(
        should_act=True,
        instruction="type exact value",
        stop=False,
        goal_completed=False,
        summary="write",
        milestone_id=milestone.id,
        execution_scope=f"milestone:{milestone.id}",
        milestone_kind="filter",
        atomic_role="write",
        action_family="input",
        target_control="Search by keyword",
    )
    write_turn = make_interactive_turn(
        index=13,
        observation_source="browser",
        supervisor_step=write_step,
        action_decision=BaseActionDecision(action=BaseAction(
            action_type="type",
            x=300,
            y=300,
            text="Minerva LumaTech V-Tee",
            description="type exact value",
        )),
        executed=True,
    )
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)
    policy = supervisor_policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(milestone)

    step = policy.step(
        Observation(
            png_bytes=b"x",
            source="browser",
            applied_filters={"Keyword": "Minerva LumaTech V-Tee"},
            tables=[{"total_records": 0}],
        ),
        goal="find product",
        history=[write_turn],
    )

    assert checker_calls == []
    assert step.goal_completed is True
    assert step.completion_status == "confirmed"


def test_zero_result_exact_search_can_finish_before_explicit_fallback(monkeypatch):
    milestone = _filter_ms(
        "在搜索框输入精确值 'Minerva LumaTech V-Tee' 并提交搜索",
        "精确筛选已应用，records-found 计数可读",
        target_values={"Keyword": "Minerva LumaTech V-Tee"},
    )
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)

    policy = supervisor_policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(milestone)
    step = policy.step(
        Observation(
            png_bytes=b"x",
            source="browser",
            applied_filters={"Keyword": "Minerva LumaTech V-Tee"},
            tables=[{"record_count": 0}],
        ),
        goal="find product",
        history=[],
    )

    assert checker_calls == []
    assert milestone.status == "done"
    assert step.goal_completed is True


# ── filter_state_satisfies_target (the gate predicate) ──────────────────────────
def test_gate_fires_when_target_chip_present():
    ms = _filter_ms("清除无关筛选，设置 Quantity From=3 且 To=3",
                    "可见筛选状态显示 Quantity: 3 - 3",
                    target_values={"Quantity": "3 - 3"})
    applied = {"Quantity": "3 - 3"}
    assert filter_state_satisfies_target(applied, ms) is True


def test_gate_does_not_fire_on_wrong_range():
    # The real failure's antidote: a 2-3 chip must NOT satisfy a 3-3 target.
    ms = _filter_ms("设置 Quantity From=3 且 To=3", target_values={"Quantity": "3 - 3"})
    assert filter_state_satisfies_target({"Quantity": "2 - 3"}, ms) is False


def test_gate_ignores_unrelated_display_column_values():
    # Salable Quantity present as a (hypothetical) chip must not be mistaken for Quantity.
    ms = _filter_ms("设置 Quantity From=3 且 To=3", target_values={"Quantity": "3 - 3"})
    applied = {"Salable Quantity": "2", "Quantity": "3 - 3"}
    assert filter_state_satisfies_target(applied, ms) is True


def test_gate_false_when_no_chips():
    ms = _filter_ms("设置 Quantity From=3 且 To=3", target_values={"Quantity": "3 - 3"})
    assert filter_state_satisfies_target(None, ms) is False
    assert filter_state_satisfies_target({}, ms) is False


def test_gate_false_when_target_unparseable():
    ms = _filter_ms("打开筛选面板")
    assert filter_state_satisfies_target({"Quantity": "3 - 3"}, ms) is False


def test_gate_matches_adapter_pair_when_free_text_parser_has_no_syntax_rule():
    # Live 20260711_012347: the adapter authoritatively returned this exact pair, but the
    # historical mini-parser did not recognize the generic "用精确值 ... 筛选" phrasing.
    ms = _filter_ms(
        "在 Attribute Code 列用精确值'size'筛选",
        "Attribute Code 精确筛选已应用，records-found 计数可读",
        target_values={"Attribute Code": "size"},
    )

    assert filter_state_satisfies_target({"Attribute Code": "size"}, ms) is True
    assert filter_chips_clean({"Attribute Code": "size"}, ms) is True


def test_structural_filter_pair_fallback_requires_both_label_and_exact_value():
    ms = _filter_ms(
        "在 Attribute Code 列用精确值'size'筛选",
        "Attribute Code 精确筛选已应用",
        target_values={"Attribute Code": "size"},
    )

    assert filter_state_satisfies_target({"Attribute Code": "color"}, ms) is False
    assert filter_state_satisfies_target({"Default Label": "size"}, ms) is False


def test_structural_filter_pair_still_rejects_unrelated_residual_filter():
    ms = _filter_ms(
        "在 Attribute Code 列用精确值'size'筛选",
        "Attribute Code 精确筛选已应用",
        target_values={"Attribute Code": "size"},
    )
    applied = {"Attribute Code": "size", "Default Label": "shoe"}

    assert filter_state_satisfies_target(applied, ms) is True
    assert filter_chips_clean(applied, ms) is False
    assert filter_residual_labels(applied, ms) == ["Default Label"]


def test_chips_not_clean_with_leaked_residual():
    ms = _filter_ms("设置 Quantity From=3 且 To=3", target_values={"Quantity": "3 - 3"})
    assert filter_chips_clean({"Keyword": "WS08", "Quantity": "3 - 3"}, ms) is False


# ── strong-path integration: the gate fires in the real policy.step(), no LLM ───
import gui_agent.core.supervisor.milestone.llm_runtime as policy_mod  # noqa: E402
import gui_agent.core.supervisor.milestone.policy as supervisor_policy_mod  # noqa: E402
from gui_agent.core.schemas import Observation  # noqa: E402

# A real (non-blank) PNG so is_loading_frame() doesn't short-circuit to a loading frame.
_FIXTURE_PNG = (
    "evals/browser/checker/screenshots/products_qty3_filter_salable_distractor.png"
)


def _qty3_filter_milestone() -> Milestone:
    return _filter_ms(
        "清除无关筛选，设置 Quantity From=3 且 To=3",
        "网格 Active filters 显示已生效筛选 Quantity: 3 - 3（控件状态达成即可，不需逐行复核库存）。",
        target_values={"Quantity": "3 - 3"},
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
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)

    pol = supervisor_policy_mod.MilestoneSupervisorPolicy()
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
        target_values={"Product": "Olivia"},
    )
    import os

    png = open(_FIXTURE_PNG, "rb").read() if os.path.exists(_FIXTURE_PNG) else b"\x89PNG\r\n\x1a\n"
    checker_calls: list[int] = []

    def _spy_run_checker(*a, **k):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)

    pol = supervisor_policy_mod.MilestoneSupervisorPolicy()
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


def test_filter_provenance_annotates_task_set_vs_initial_state():
    # WebArena 505 (20260708_195215): the checker labeled the chips set by THIS run's earlier
    # milestones "残留" and cleared them every step. With the run-start baseline, initial-state
    # chips and task-set chips get distinct deterministic annotations. NO provenance guessing:
    # initial-state chips are just "how the environment was", not attributed to any prior task.
    block = applied_filter_state_block(
        {"Keyword": "Gobi", "Name": "Aeon capri"},
        None,
        initial_filters={"Keyword": "Gobi"},
    )
    assert block is not None
    text = block.render()
    assert "任务开始时已生效" in text            # Keyword: Gobi — observed initial environment state
    assert "本任务步骤设置" in text              # Name: Aeon capri — this run's own scope
    assert "不改动筛选状态" in text              # non-filter milestones leave filter state alone
    assert "上一任务" not in text                # no attribution to a "previous task"
    assert "残留" not in text                    # no hygiene framing


def test_filter_provenance_absent_without_baseline():
    # No baseline captured yet → keep the legacy rendering, no state-attribution claims.
    block = applied_filter_state_block({"Name": "Aeon capri"}, None)
    assert block is not None
    text = block.render()
    assert "任务开始时已生效" not in text
    assert "本任务步骤设置" not in text


def test_policy_captures_first_applied_filters_snapshot(monkeypatch):
    from gui_agent.core.schemas import SupervisorStep
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    pol = MilestoneSupervisorPolicy()
    ms = Milestone.model_validate({
        "id": "m1", "name": "进入 Products 页", "description": "", "success_condition": "列表可见",
        "kind": "navigation",
    })
    pol.reseed(ms)
    monkeypatch.setattr(pol, "_run_single_turn", lambda *a, **k: SupervisorStep(
        should_act=False, instruction=None, stop=False, goal_completed=False, summary="", milestone_id="m1",
    ))
    # Turn 1: no applied-filters channel (dashboard) → baseline not captured.
    pol.step(Observation(png_bytes=b"x", source="browser"), goal="g", history=[])
    assert pol._initial_filters is None
    # Turn 2: first grid observation with residue chips → baseline captured once.
    pol.step(
        Observation(png_bytes=b"x", source="browser", applied_filters={"Keyword": "Gobi"}),
        goal="g", history=[],
    )
    assert pol._initial_filters == {"Keyword": "Gobi"}
    # Turn 3: task's own filter appears — baseline must NOT be overwritten.
    pol.step(
        Observation(png_bytes=b"x", source="browser", applied_filters={"Name": "Aeon capri"}),
        goal="g", history=[],
    )
    assert pol._initial_filters == {"Keyword": "Gobi"}


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
    ms = _filter_ms(
        "设置目标筛选状态",
        target_values={"Quantity": "3 - 3"},
    )
    applied = {"Quantity": "3 - 3", "Keyword": "WS08"}
    assert filter_residual_labels(applied, ms) == ["Keyword"]


def test_no_residuals_when_live_state_exactly_matches_contract():
    ms = _filter_ms("设置筛选状态", target_values={"Quantity": "3 - 3"})
    assert filter_residual_labels({"Quantity": "3 - 3"}, ms) == []


def test_preserved_entity_scope_filter_is_not_residual_when_adding_filter():
    ms = _filter_ms(
        "保留 Sarah Miller 客户结果范围，追加 Status=Pending 筛选",
        "可见筛选状态同时包含 Keyword: Sarah Miller 和 Status: Pending",
        target_values={"Keyword": "Sarah Miller", "Status": "Pending"},
    )
    applied = {"Keyword": "Sarah Miller", "Status": "Pending"}
    assert filter_residual_labels(applied, ms) == []
    assert filter_chips_clean(applied, ms) is True


def test_preserved_scope_can_match_distinctive_partial_token():
    ms = _filter_ms(
        "保留 Grace 客户结果范围，追加 Status=Pending 筛选",
        "可见筛选状态同时包含 Grace 客户范围和 Status: Pending",
        target_values={"Keyword": "Grace Nguyen", "Status": "Pending"},
    )
    applied = {"Keyword": "Grace Nguyen", "Status": "Pending"}
    assert filter_residual_labels(applied, ms) == []
    assert filter_chips_clean(applied, ms) is True


def test_preserved_scope_does_not_keep_residual_by_target_value_overlap_only():
    ms = _filter_ms(
        "保留客户结果范围，追加 Status=Pending 筛选",
        "可见筛选状态包含 Status: Pending",
        target_values={"Status": "Pending"},
    )
    applied = {"Keyword": "Pending", "Status": "Pending"}
    assert filter_residual_labels(applied, ms) == ["Keyword"]
    assert filter_chips_clean(applied, ms) is False


def test_undeclared_keyword_scope_is_residual_when_setting_column_filter():
    ms = _filter_ms("设置 Status=Pending 筛选", target_values={"Status": "Pending"})
    applied = {"Keyword": "Sarah Miller", "Status": "Pending"}
    assert filter_residual_labels(applied, ms) == ["Keyword"]
    assert filter_chips_clean(applied, ms) is False


def test_unparseable_non_nofilter_target_yields_no_residuals():
    # Can't diff without an intent → return [] (don't guess / don't blanket-clear).
    ms = _filter_ms("打开筛选面板")
    assert filter_residual_labels({"Keyword": "WS08"}, ms) == []


def test_runtime_filter_receipt_can_diff_residuals_for_legacy_program():
    ms = _filter_ms("在 Name 字段筛选 Nona")
    intent = RuntimeFilterIntent("Name", "Nona")

    assert filter_residual_labels(
        {"Keyword": "WS08", "Name": "Nona"}, ms, intent
    ) == ["Keyword"]


def test_residuals_empty_when_no_applied_filters():
    ms = _filter_ms("设置 Quantity From=3 且 To=3", target_values={"Quantity": "3 - 3"})
    assert filter_residual_labels(None, ms) == []


def test_declared_keyword_filter_treats_other_filter_as_residual():
    ms = _filter_ms(
        "应用关键词筛选",
        target_values={"Keyword": "WS08"},
    )
    assert filter_residual_labels({"Quantity": "3 - 3"}, ms) == ["Quantity"]
    assert filter_residual_labels(
        {"Keyword": "WS08", "Quantity": "3 - 3"}, ms
    ) == ["Quantity"]


def test_quantity_filter_contract_keeps_only_declared_dimension():
    ms = _filter_ms(
        "设置 Quantity 筛选",
        target_values={"Quantity": "3 - 3"},
    )
    assert filter_residual_labels({"Quantity": "3 - 3", "Keyword": "WS08"}, ms) == ["Keyword"]
