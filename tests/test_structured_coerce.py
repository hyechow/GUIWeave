"""Tolerate an LLM returning ``list[str]`` fields as bare strings.

DashScope ``json_object`` mode occasionally emits a ``list[str]`` field as a single
string (e.g. ``{"missing_evidence": "需要看到..."}``). Without a before-validator the
primary ``model_validate`` raises ``ValidationError`` → ``invoke_structured`` falls
back to a slow plain-text reparse (1-2 extra LLM calls; see log 20260616_200258
Turn5/6 checker=10.58s). The coerce validator wraps the string into a one-element list
so the primary parse succeeds on the first try.
"""

from __future__ import annotations

from gui_agent.core.supervisor.statement.schemas import (
    _SelectorResult,
    _SingleCheckResult,
)
from gui_agent.adapters.browser.supervisor.statement.prompts import BrowserPlanResult


def test_single_check_schema_requires_explicit_effect_status():
    required = _SingleCheckResult.model_json_schema().get("required", [])

    assert "effect_status" in required


def test_single_check_migrates_legacy_outcome_status_at_model_boundary():
    result = _SingleCheckResult.model_validate({
        "status": "in_progress",
        "outcome_status": "unverified",
        "reason": "legacy replay frame",
        "summary": "still running",
    })

    assert result.effect_status == "unverified"


def test_single_check_still_requires_explicit_effect_status():
    # effect_status is the business-target verdict; omission must fall back to reparse for an
    # honest answer rather than be inferred. (Deliberate invariant — do not relax without intent.)
    import pytest
    with pytest.raises(Exception):
        _SingleCheckResult.model_validate({"status": "done", "reason": "x", "summary": "y"})


def test_single_check_keeps_explicit_effect_status_when_present():
    r = _SingleCheckResult.model_validate({
        "status": "done",
        "reason": "x",
        "summary": "y",
        "effect_status": "confirmed",
    })
    assert r.effect_status == "confirmed"


def test_single_check_fills_missing_summary():
    # summary is a display field; tolerate its omission on the primary parse.
    r = _SingleCheckResult.model_validate({
        "status": "in_progress",
        "reason": "x",
        "effect_status": "unmet",
    })
    assert r.summary == ""


def test_single_check_wraps_bare_string_missing_evidence():
    # The exact failure shape from log 20260616_200258 Turn5.
    r = _SingleCheckResult.model_validate({
        "status": "in_progress",
        "effect_status": "unverified",
        "reason": "x",
        "summary": "y",
        "missing_evidence": "需要看到文件选择器或上传成功的提示",
    })
    assert r.missing_evidence == ["需要看到文件选择器或上传成功的提示"]


def test_single_check_wraps_bare_string_for_all_three_lists():
    r = _SingleCheckResult.model_validate({
        "status": "done",
        "effect_status": "confirmed",
        "reason": "x",
        "summary": "y",
        "missing_evidence": "缺A",
        "visible_evidence": "见B",
        "issues": "问题C",
    })
    assert r.missing_evidence == ["缺A"]
    assert r.visible_evidence == ["见B"]
    assert r.issues == ["问题C"]


def test_single_check_handles_none_and_list_inputs():
    r = _SingleCheckResult.model_validate({
        "status": "done",
        "effect_status": "confirmed",
        "reason": "x",
        "summary": "y",
        "missing_evidence": None,
        "visible_evidence": ["a", "b"],
    })
    assert r.missing_evidence == []
    assert r.visible_evidence == ["a", "b"]


def test_single_check_keeps_a_normal_list_untouched():
    r = _SingleCheckResult.model_validate({
        "status": "in_progress",
        "effect_status": "unverified",
        "reason": "x",
        "summary": "y",
        "missing_evidence": ["a", "b"],
    })
    assert r.missing_evidence == ["a", "b"]


def test_selector_wraps_bare_string_section_ids():
    r = _SelectorResult.model_validate({"section_ids": "s07"})
    assert r.section_ids == ["s07"]


def test_selector_keeps_a_normal_list_untouched():
    r = _SelectorResult.model_validate({"section_ids": ["s07", "s26"]})
    assert r.section_ids == ["s07", "s26"]


def _browser_plan(**overrides):
    base = {
        "instruction": "在价格框填入 29.99",
        "summary": "写入目标价",
        "atomic_role": "write",
        "action_family": "input",
    }
    base.update(overrides)
    return base


def test_browser_plan_coerces_null_target_value_to_empty():
    # The exact reported failure: json_object emitted "target_value": null for an optional
    # str field, failing the primary parse and triggering the slow plain-text reparse.
    r = BrowserPlanResult.model_validate(_browser_plan(target_value=None))
    assert r.target_value == ""


def test_browser_plan_coerces_null_target_control_to_empty():
    r = BrowserPlanResult.model_validate(_browser_plan(target_control=None))
    assert r.target_control == ""


def test_browser_plan_stringifies_numeric_target_value():
    # A numeric target_value (price/quantity) emitted as a JSON number would otherwise
    # fail the same str validation — same class of bug.
    r = BrowserPlanResult.model_validate(_browser_plan(target_value=29.99))
    assert r.target_value == "29.99"


def test_browser_plan_absent_optional_strings_keep_default():
    r = BrowserPlanResult.model_validate(_browser_plan())
    assert r.target_value == ""
    assert r.target_control == ""


def test_browser_plan_does_not_coerce_required_null_instruction():
    # A null on a REQUIRED string is a real planner failure; the fallback reparse is the
    # correct response, so the coerce must NOT mask it.
    import pytest
    with pytest.raises(Exception):
        BrowserPlanResult.model_validate({
            "instruction": None,
            "summary": "x",
        })
