from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserActionDecision
from manager_protocol.action_tools import action_tools, decision_from_tool_call
from manager_protocol.run import load_source_case, load_suite, resolve_screenshot, score_action
from manager_protocol.state_action_run import (
    StateAssessment,
    diagnose_tool_attempt,
    load_replay_case,
    load_suite as load_state_action_suite,
    score_action as score_replay_action,
    score_state,
)


def test_browser_tools_have_distinct_names_and_no_action_type_argument() -> None:
    tools = action_tools("browser")

    assert len({tool.name for tool in tools}) == len(tools)
    assert {"navigate", "type_text", "upload_file", "scroll"} <= {
        tool.name for tool in tools
    }
    assert all(
        "action_type" not in tool.args_model.model_json_schema().get("properties", {})
        for tool in tools
    )


def test_navigate_tool_normalizes_through_browser_action_schema() -> None:
    decision = decision_from_tool_call(
        "browser",
        BrowserActionDecision,
        "navigate",
        {"url": "https://feishu.cn"},
    )

    assert decision.action.action_type == "navigate"
    assert decision.action.url == "https://feishu.cn"
    assert decision.action.description == "导航到 https://feishu.cn"


def test_case_scoring_supports_meta_expectations() -> None:
    decision = decision_from_tool_call(
        "browser",
        BrowserActionDecision,
        "navigate",
        {"url": "https://www.feishu.cn"},
    )

    assert score_action(
        decision.action,
        {"action_type": "navigate", "url_contains": "feishu.cn"},
    ) == []


def test_iphone_legacy_screenshot_path_falls_back_to_source_directory() -> None:
    suite = load_suite()
    source, case = load_source_case(suite, "iphone", "0")

    screenshot = resolve_screenshot(source, case)

    assert screenshot.name == "scroll_down.png"
    assert screenshot.is_file()


def test_thinking_tool_variant_uses_explicit_auto_choice_for_tokenplan() -> None:
    suite = load_suite()
    variant = next(item for item in suite["variants"] if item["id"] == "tool_call_on")

    assert variant["thinking"] is True
    assert variant["tool_choice"] == "auto"


def test_state_action_replay_reads_passed_webarena_turn_without_copying_image() -> None:
    suite = load_state_action_suite()
    case = load_replay_case(suite, suite["cases"][1])

    assert case.screenshot_path.name == "screenshot_turn_4.png"
    assert case.recorded_instruction == (
        "在 Columns 面板中，找到并点击 'Customer Email' 复选框以选中它。"
    )
    assert case.screenshot


def test_state_action_replay_scores_state_and_target_region_separately() -> None:
    state = StateAssessment(
        status="in_progress",
        summary="Columns 已打开，Customer Email 尚未选中",
        next_instruction="点击 Customer Email",
    )
    expected_state = {
        "status": "in_progress",
        "keywords": ["Columns", "Customer Email"],
    }
    action = BrowserActionDecision.model_validate(
        {"action": {"action_type": "tap", "x": 720, "y": 491}}
    ).action
    expected_action = {
        "action_type": "tap",
        "point": [720.1, 490.7],
        "target_box": [695, 840, 465, 520],
    }

    assert score_state(state, expected_state)["ok"] is True
    assert score_replay_action(action, expected_action)["target_hit"] is True


def test_state_action_replay_diagnoses_point_pair_in_invalid_scalar_argument() -> None:
    expected = {
        "action_type": "tap",
        "point": [720.1, 490.7],
        "target_box": [695, 840, 465, 520],
    }

    diagnostic = diagnose_tool_attempt(
        [{"name": "tap", "args": {"x": [710, 490]}}],
        expected,
    )

    assert diagnostic["tool_name_correct"] is True
    assert diagnostic["diagnostic_target_hit"] is True
    assert diagnostic["diagnostic_action_match"] is True
    assert diagnostic["coordinate_candidates"] == [[710.0, 490.0]]

    string_diagnostic = diagnose_tool_attempt(
        [{
            "name": "type_text",
            "args": {
                "x": "[720, 490]",
                "y": "[720, 490]",
                "text": "hello",
            },
        }],
        {
            "action_type": "type",
            "tool_name": "type_text",
            "fields": {"text": "hello"},
            "target_box": [695, 840, 465, 520],
        },
    )
    assert string_diagnostic["diagnostic_action_match"] is True


def test_state_action_replay_scores_type_text_and_non_coordinate_scroll() -> None:
    typed = BrowserActionDecision.model_validate({
        "action": {
            "action_type": "type",
            "x": 375,
            "y": 628,
            "text": "05/01/2021",
        }
    }).action
    typed_expected = {
        "action_type": "type",
        "tool_name": "type_text",
        "fields": {"text": "05/01/2021"},
        "point": [375, 628],
        "target_box": [310, 440, 600, 655],
    }
    scrolled = BrowserActionDecision.model_validate({
        "action": {
            "action_type": "scroll",
            "direction": "down",
            "target_area": "main_content",
        }
    }).action
    scroll_expected = {
        "action_type": "scroll",
        "fields": {"direction": "down", "target_area": "main_content"},
    }

    assert score_replay_action(typed, typed_expected)["ok"] is True
    scroll_score = score_replay_action(scrolled, scroll_expected)
    assert scroll_score["ok"] is True
    assert scroll_score["target_hit"] is None
    assert diagnose_tool_attempt(
        [{"name": "type_text", "args": {"x": [375, 628], "text": "05/01/2021"}}],
        typed_expected,
    )["diagnostic_action_match"] is True


def test_all_state_action_replay_cases_reference_official_passed_runs() -> None:
    suite = load_state_action_suite()

    cases = [load_replay_case(suite, spec) for spec in suite["cases"]]

    assert len(cases) == 8
    assert {case.spec["expected_action"]["action_type"] for case in cases} == {
        "tap",
        "type",
        "select_option",
        "scroll",
    }
    assert all(case.screenshot for case in cases)


def test_state_assessment_carries_semantic_action_handoff() -> None:
    state = StateAssessment(
        status="in_progress",
        summary="Page Title still has the old value",
        next_instruction="Replace Page Title with the requested text",
        action_family="input",
        target_control="Page Title",
        target_value="New title",
        expected_result="Page Title displays New title",
    )

    assert state.action_family == "input"
    assert state.target_value == "New title"


def test_state_action_suite_records_explicit_tool_choice() -> None:
    suite = load_state_action_suite()

    assert suite["sampling"]["thinking"] is True
    assert suite["sampling"]["tool_choice"] == "auto"
