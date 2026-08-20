from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.control_grounding import (
    _compatible_with_action,
    _matches_described_control_type,
    ground_action_to_nearest_control,
    ground_rendered_action,
    rendered_target_evidence,
    resolve_native_control_action,
    resolve_semantic_action,
    semantic_target_evidence,
)


def test_coordinate_grounding_fails_open_when_nearby_controls_are_ambiguous() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=500,
        y=500,
        description="Tap one visible action in the center content area",
    ))
    controls = [
        {
            "kind": "button",
            "label": "Left action",
            "rect": {"x": 480, "y": 500, "w": 30, "h": 30},
        },
        {
            "kind": "button",
            "label": "Right action",
            "rect": {"x": 520, "y": 500, "w": 30, "h": 30},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1000, 1000),
    )

    assert decision is visual
    assert decision.action.snap is None


def test_coordinate_grounding_uses_unique_visible_name_to_resolve_nearby_rows() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=292,
        y=140,
        description="Tap the Orders link in the SALES submenu dropdown",
    ))
    controls = [
        {
            "kind": "a",
            "label": "Orders",
            "rect": {"x": 173, "y": 118, "w": 238, "h": 44},
        },
        {
            "kind": "a",
            "label": "Invoices",
            "rect": {"x": 173, "y": 164, "w": 238, "h": 44},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1280, 963),
    )

    assert (decision.action.x, decision.action.y) == (173.0, 118.0)
    assert decision.action.snap == {
        "method": "control_semantic_geometry",
        "original": [292.0, 140.0],
        "snapped": [173.0, 118.0],
        "info": "Orders",
    }


def test_coordinate_grounding_does_not_use_partial_visible_name_match() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=500,
        y=500,
        description="Tap the Orders action",
    ))
    controls = [
        {
            "kind": "button",
            "label": "Order",
            "rect": {"x": 480, "y": 500, "w": 30, "h": 30},
        },
        {
            "kind": "button",
            "label": "Invoices",
            "rect": {"x": 520, "y": 500, "w": 30, "h": 30},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1000, 1000),
    )

    assert decision is visual
    assert decision.action.snap is None


def test_named_result_row_repairs_a_large_miss_to_its_button() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=320,
        y=140,
        description="Tap the North Harbor result row in the visible results panel",
    ))
    controls = [
        {
            "kind": "text_input",
            "label": "Search locations",
            "rect": {"x": 320, "y": 140, "w": 300, "h": 48},
        },
        {
            "kind": "button",
            "label": "North Harbor",
            "rect": {"x": 320, "y": 220, "w": 300, "h": 40},
        },
    ]

    decision = ground_action_to_nearest_control(visual, controls)

    assert (decision.action.x, decision.action.y) == (320.0, 220.0)
    assert decision.action.snap["method"] == "control_semantic_geometry"


def test_coordinate_grounding_fails_open_when_visible_name_is_duplicated() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=500,
        y=500,
        description="Tap the Orders link",
    ))
    controls = [
        {
            "kind": "a",
            "label": "Orders",
            "rect": {"x": 480, "y": 500, "w": 30, "h": 30},
        },
        {
            "kind": "a",
            "label": "Orders",
            "rect": {"x": 520, "y": 500, "w": 30, "h": 30},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1000, 1000),
    )

    assert decision is visual
    assert decision.action.snap is None


def test_coordinate_grounding_does_not_cross_large_control_to_distant_center() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=850,
        y=500,
        description="Tap the Orders surface",
    ))
    controls = [{
        "kind": "a",
        "label": "Orders",
        "rect": {"x": 500, "y": 500, "w": 800, "h": 40},
    }]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1000, 1000),
    )

    assert decision is visual
    assert decision.action.snap is None


def test_explicit_target_phrase_repairs_large_coordinate_miss_despite_neighbor_name() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=919,
        y=270,
        description=(
            "Tap the Filters button in the toolbar between the search field and "
            "Default View dropdown"
        ),
    ))
    controls = [
        {
            "kind": "button",
            "label": "Filters",
            "rect": {"x": 608, "y": 277, "w": 101, "h": 34},
        },
        {
            "kind": "button",
            "label": "Default View",
            "rect": {"x": 733, "y": 277, "w": 146, "h": 34},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1280, 963),
    )

    assert (decision.action.x, decision.action.y) == (608.0, 277.0)
    assert decision.action.snap == {
        "method": "control_semantic_geometry",
        "original": [919.0, 270.0],
        "snapped": [608.0, 277.0],
        "info": "Filters",
    }


def test_first_explicit_target_wins_over_later_different_control_family() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=159,
        y=292,
        description=(
            "Search button located in the filter toolbar above the grid, "
            "to the left of Reset Filter link"
        ),
    ))
    controls = [
        {
            "kind": "button",
            "label": "Search",
            "rect": {"x": 122, "y": 276, "w": 75, "h": 33},
        },
        {
            "kind": "a",
            "label": "Reset Filter",
            "rect": {"x": 195, "y": 276, "w": 106, "h": 33},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1280, 963),
    )

    assert (decision.action.x, decision.action.y) == (122.0, 276.0)
    assert decision.action.snap == {
        "method": "control_semantic_geometry",
        "original": [159.0, 292.0],
        "snapped": [122.0, 276.0],
        "info": "Search",
    }


def test_explicit_text_input_phrase_disambiguates_login_fields() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=500,
        y=490,
        text="secret",
        description="Password text input field below the Username field",
    ))
    controls = [
        {
            "kind": "text_input",
            "label": "Username",
            "rect": {"x": 500, "y": 490, "w": 400, "h": 40},
        },
        {
            "kind": "password_input",
            "label": "Password",
            "rect": {"x": 500, "y": 580, "w": 400, "h": 40},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1280, 963),
    )

    assert (decision.action.x, decision.action.y) == (500.0, 580.0)
    assert decision.action.snap["info"] == "Password"


def test_grounding_respects_editable_control_family_with_mixed_language_labels() -> None:
    controls = [
        {"kind": "textarea", "label": "搜索",
         "rect": {"x": 484, "y": 440, "w": 447, "h": 50}},
        {"kind": "submit_input", "label": "Portal 搜索", "value": "Portal 搜索",
         "rect": {"x": 457, "y": 531, "w": 111, "h": 36}},
    ]
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=300,
        y=425,
        text="query",
        description="Portal search textarea input field labeled '搜索' in the center",
    ))
    decision = ground_action_to_nearest_control(
        visual, controls, viewport_size=(1280, 800),
    )
    assert (decision.action.x, decision.action.y) == (484.0, 440.0)
    assert decision.action.snap["info"] == "搜索"

    aria = {"kind": "aria_combobox", "label": "Assignee"}
    assert _compatible_with_action(aria, "type")
    assert _matches_described_control_type("Assignee combobox", aria)

    rating = {"kind": "rating", "label": "Your Rating", "options": ["1", "2"]}
    assert _compatible_with_action(rating, "select_option")
    assert not _compatible_with_action(rating, "type")

    for kind in (
        "submit_input", "button_input", "checkbox_input", "radio_input",
        "file_input", "hidden_input", "color_input", "range_input",
    ):
        visual = BrowserActionDecision(action=BrowserAction(
            action_type="type", x=100, y=100, text="value",
            description="Type value into the visible text field",
        ))
        decision = ground_action_to_nearest_control(
            visual, [{"kind": kind, "label": "Other",
                      "rect": {"x": 150, "y": 100, "w": 40, "h": 20}}],
            viewport_size=(1000, 1000),
        )
        assert decision is visual, kind


def test_semantic_grid_checkbox_label_repairs_adjacent_row_miss() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=156,
        y=696,
        description="Select the size row checkbox in the Select Attribute grid",
    ))
    controls = [
        {
            "kind": "checkbox_input",
            "label": "sale",
            "rect": {"x": 156, "y": 696, "w": 13, "h": 13},
        },
        {
            "kind": "checkbox_input",
            "label": "size",
            "rect": {"x": 156, "y": 740, "w": 13, "h": 13},
        },
    ]

    decision = ground_action_to_nearest_control(
        visual,
        controls,
        viewport_size=(1280, 963),
    )

    assert (decision.action.x, decision.action.y) == (156.0, 740.0)
    assert decision.action.snap["info"] == "size"


def _description_controls() -> list[dict]:
    return [
        {
            "label": "Description",
            "name": "optiontext[value][option_19][0]",
            "kind": "text_input",
            "value": "38",
            "group_id": "collection:19",
            "group_field": "Admin",
            "rect": {"x": 578, "y": 632, "w": 217, "h": 33},
        },
        {
            "label": "Description",
            "name": "optiontext[value][option_20][0]",
            "kind": "text_input",
            "value": "",
            "group_id": "collection:20",
            "group_field": "Admin",
            "rect": {"x": 578, "y": 668, "w": 217, "h": 33},
        },
        {
            "label": "Description",
            "name": "optiontext[value][option_20][1]",
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


def test_rendered_input_uses_exact_form_control_ref() -> None:
    visual = BrowserActionDecision(action=BrowserAction(
        action_type="type", x=820, y=175, text="XXXL", description="输入 XXXL"
    ))

    decision = ground_rendered_action(
        visual,
        _description_controls(),
        target_control="Admin Description input in the new blank row",
        target_value="XXXL",
        target_group_id="",
        action_family="input",
        target_ref="optiontext[value][option_20][0]",
    )

    assert (decision.action.x, decision.action.y) == (578, 668)
    assert decision.action.snap["info"] == "Admin Description"


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


def test_unique_semantic_action_target_is_exposed_to_visual_policy() -> None:
    evidence = semantic_target_evidence(
        [
            {"role": "button", "key": "Add New Attribute"},
            {"role": "button", "key": "Search"},
        ],
        target_control="Search",
        action_family="activate",
    )

    assert "matched_document_target='Search'" in evidence
    assert "不证明它在截图视口内" in evidence
    assert "不得用其他可见按钮" in evidence


def test_semantic_activate_clicks_link_instead_of_opening_its_url() -> None:
    decision = resolve_semantic_action(
        [{
            "role": "link",
            "key": "STORES",
            "url": "https://example.test/admin/dashboard/#",
            "ref": 252808,
            "in_viewport": True,
            "point": {"x": 35.0, "y": 561.0},
        }],
        target_control="STORES",
        target_ref="252808",
        action_family="activate",
        instruction="Expand the STORES menu.",
    )

    assert decision is not None
    assert decision.action.action_type == "tap"
    assert (decision.action.x, decision.action.y) == (35.0, 561.0)
    assert decision.action.url is None


def test_semantic_ref_owns_identity_when_label_has_decorative_glyphs() -> None:
    decision = resolve_semantic_action(
        [{
            "role": "link",
            "key": "\ue608 CATALOG",
            "url": "https://example.test/admin/edit/#",
            "ref": 273644,
            "in_viewport": True,
            "point": {"x": 35.0, "y": 239.0},
        }],
        target_control="Catalog",
        target_ref="273644",
        action_family="activate",
    )

    assert decision is not None
    assert decision.action.action_type == "tap"
    assert (decision.action.x, decision.action.y) == (35.0, 239.0)


def test_semantic_navigate_opens_exact_link_url() -> None:
    decision = resolve_semantic_action(
        [{
            "role": "link",
            "key": "Product Attributes",
            "url": "https://example.test/admin/attributes/",
            "ref": 42,
            "in_viewport": True,
            "point": {"x": 100.0, "y": 200.0},
        }],
        target_control="Product Attributes",
        target_ref="42",
        action_family="navigate",
    )

    assert decision is not None
    assert decision.action.action_type == "navigate"
    assert decision.action.url == "https://example.test/admin/attributes/"


def test_semantic_activate_navigates_document_link_without_coordinate_click() -> None:
    decision = resolve_semantic_action(
        [{
            "role": "link",
            "key": "Edit",
            "url": "https://example.test/admin/reviews/edit/351/",
            "ref": 351,
            "in_viewport": True,
            "point": {"x": 945.0, "y": 561.0},
        }],
        target_control="Edit",
        target_ref="351",
        action_family="activate",
    )

    assert decision is not None
    assert decision.action.action_type == "navigate"
    assert decision.action.url == "https://example.test/admin/reviews/edit/351/"


def test_semantic_activate_transports_unique_offscreen_target_before_click() -> None:
    decision = resolve_semantic_action(
        [{
            "role": "button",
            "key": "Edit Configurations",
            "ref": 415757,
            "in_viewport": False,
            "point": {"x": 913.0, "y": 1603.0},
        }],
        target_control="Edit Configurations",
        target_ref="",
        action_family="activate",
        instruction="Click Edit Configurations",
    )

    assert decision is not None
    assert decision.action.action_type == "scroll_to_ref"
    assert decision.action.target_ref == 415757


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


def test_offscreen_form_control_uses_bounded_directional_scroll() -> None:
    decision = resolve_native_control_action(
        [{
            "label": "Material",
            "kind": "native_select",
            "rect": {"x": 414, "y": -27, "w": 250, "h": 176},
        }],
        target_control="Material",
        target_value="",
        target_group_id="",
        action_family="iterate",
        instruction="将 Material 字段带入视口",
    )

    assert decision is not None
    assert decision.action.action_type == "scroll"
    assert decision.action.direction == "up"
    assert decision.action.amount == "medium"


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
