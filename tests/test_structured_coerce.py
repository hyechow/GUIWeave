"""Tolerate an LLM returning ``list[str]`` fields as bare strings.

DashScope ``json_object`` mode occasionally emits a ``list[str]`` field as a single
string (e.g. ``{"missing_evidence": "需要看到..."}``). Without a before-validator the
primary ``model_validate`` raises ``ValidationError`` → ``invoke_structured`` falls
back to a slow plain-text reparse (1-2 extra LLM calls; see log 20260616_200258
Turn5/6 checker=10.58s). The coerce validator wraps the string into a one-element list
so the primary parse succeeds on the first try.
"""

from __future__ import annotations

from gui_agent.core.supervisor.milestone.schemas import _SelectorResult, _SingleCheckResult


def test_single_check_schema_requires_explicit_outcome_status():
    required = _SingleCheckResult.model_json_schema().get("required", [])

    assert "outcome_status" in required


def test_single_check_wraps_bare_string_missing_evidence():
    # The exact failure shape from log 20260616_200258 Turn5.
    r = _SingleCheckResult.model_validate({
        "status": "in_progress",
        "outcome_status": "unverified",
        "reason": "x",
        "summary": "y",
        "missing_evidence": "需要看到文件选择器或上传成功的提示",
    })
    assert r.missing_evidence == ["需要看到文件选择器或上传成功的提示"]


def test_single_check_wraps_bare_string_for_all_three_lists():
    r = _SingleCheckResult.model_validate({
        "status": "done",
        "outcome_status": "confirmed",
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
        "outcome_status": "confirmed",
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
        "outcome_status": "unverified",
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
