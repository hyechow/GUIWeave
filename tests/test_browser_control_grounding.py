from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.control_grounding import (
    ground_rendered_action,
    rendered_target_evidence,
    resolve_native_control_action,
)


def _description_controls() -> list[dict]:
    return [
        {
            "label": "Description",
            "kind": "text_input",
            "value": "38",
            "group_id": "collection:19",
            "group_field": "Admin",
            "rect": {"x": 578, "y": 632, "w": 217, "h": 33},
        },
        {
            "label": "Description",
            "kind": "text_input",
            "value": "",
            "group_id": "collection:20",
            "group_field": "Admin",
            "rect": {"x": 578, "y": 668, "w": 217, "h": 33},
        },
        {
            "label": "Description",
            "kind": "text_input",
            "value": "",
            "group_id": "collection:20",
            "group_field": "Default Store View",
            "rect": {"x": 821, "y": 668, "w": 217, "h": 33},
        },
    ]


def test_rendered_input_is_grounded_only_after_visual_policy_decides_type() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=820,
        y=175,
        text="XXXL",
        description="在 Admin Description 输入框填入 XXXL",
    ))
    decision = ground_rendered_action(
        visual,
        _description_controls(),
        target_control="Admin Description (Swatch row)",
        target_value="XXXL",
        target_group_id="collection:20",
        action_family="input",
    )

    assert decision.action.action_type == "type"
    assert decision.action.text == "XXXL"
    assert decision.action.x == 578
    assert decision.action.y == 668
    assert decision.action.snap["method"] == "semantic_dom"
    assert decision.action.snap["info"] == "Admin Description"


def test_rendered_input_is_not_grounded_without_explicit_group_identity() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type", x=820, y=175, text="XXXL", description="输入 XXXL"
    ))
    decision = ground_rendered_action(
        visual,
        _description_controls(),
        target_control="Admin Description",
        target_value="XXXL",
        target_group_id="",
        action_family="input",
    )

    assert decision is visual


def test_generic_label_does_not_choose_one_grouped_column() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type", x=820, y=175, text="XXXL", description="输入 XXXL"
    ))
    decision = ground_rendered_action(
        visual,
        _description_controls(),
        target_control="Description",
        target_value="XXXL",
        target_group_id="collection:20",
        action_family="input",
    )

    assert decision is visual


def test_offscreen_rendered_target_does_not_synthesize_scroll() -> None:
    controls = [{
        "label": "Description",
        "kind": "text_input",
        "value": "",
        "group_id": "collection:20",
        "group_field": "Admin",
        "in_viewport": False,
        "viewport_pos": "below",
        "rect": {"x": 578, "y": 1200, "w": 217, "h": 33},
    }]

    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type", x=500, y=500, text="XXXL", description="输入 XXXL"
    ))
    decision = ground_rendered_action(
        visual,
        controls,
        target_control="Admin Description",
        target_value="XXXL",
        target_group_id="collection:20",
        action_family="input",
    )

    assert decision is visual


def test_native_select_can_bypass_visual_policy() -> None:
    controls = [{
        "label": "Status",
        "kind": "native_select",
        "rect": {"x": 700, "y": 400},
    }]

    decision = resolve_native_control_action(
        controls,
        target_control="Status",
        target_value="Complete",
        target_group_id="__form__",
        action_family="select",
    )

    assert decision is not None
    assert decision.action.action_type == "select_option"
    assert decision.action.text == "Complete"


def test_rendered_input_never_bypasses_visual_policy() -> None:
    decision = resolve_native_control_action(
        _description_controls(),
        target_control="Admin Description",
        target_value="XXXL",
        target_group_id="collection:20",
        action_family="input",
    )

    assert decision is None


def test_rendered_target_evidence_keeps_adjacent_same_value_out_of_target_state() -> None:
    controls = [
        {
            "label": "Description",
            "group_field": "Admin",
            "group_id": "collection:19",
            "kind": "text_input",
            "value": "XXXL",
            "rect": {"x": 578, "y": 665},
            "in_viewport": True,
        },
        {
            "label": "Swatch",
            "group_field": "Admin",
            "group_id": "collection:19",
            "kind": "text_input",
            "value": "",
            "rect": {"x": 457, "y": 665},
            "in_viewport": True,
        },
    ]

    evidence = rendered_target_evidence(
        controls,
        target_control="Admin Swatch",
        target_value="XXXL",
        target_group_id="collection:19",
        action_family="input",
    )

    assert "matched_control='Admin Swatch'" in evidence
    assert "current_value=''" in evidence
    assert "requested_value='XXXL'" in evidence
    assert "center=(457,665)" in evidence


def test_rendered_combobox_never_bypasses_visual_policy() -> None:
    decision = resolve_native_control_action(
        [{
            "label": "Status",
            "kind": "combobox",
            "rect": {"x": 700, "y": 400},
        }],
        target_control="Status",
        target_value="Complete",
        target_group_id="__form__",
        action_family="select",
    )

    assert decision is None


def test_navigation_target_falls_back_instead_of_becoming_select_option() -> None:
    controls = [{
        "label": "Attribute Code",
        "kind": "text_input",
        "value": "size",
        "group_id": "attribute-grid:1",
        "group_field": "Attribute Code",
        "rect": {"x": 167, "y": 393},
    }]

    decision = resolve_native_control_action(
        controls,
        target_control="Attribute Code",
        target_value="",
        target_group_id="",
        action_family="navigate",
        instruction="打开 size 属性编辑页",
    )

    assert decision is None
