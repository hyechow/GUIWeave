from __future__ import annotations

import types

from gui_agent.adapters.browser.device import _FORM_VALUE_FINGERPRINT_JS
from gui_agent.adapters.browser.form_reader import (
    form_controls_js,
    normalize_form_control_state,
    normalize_form_control_snapshot,
    normalize_form_controls,
)
from gui_agent.adapters.browser.perception import BrowserPerception
from gui_agent.core.schemas import Observation


def test_form_controls_js_is_serialized_expression():
    js = form_controls_js()
    assert "querySelectorAll" in js
    assert "input,select,textarea" in js
    assert "native_select" in js
    assert "section_toggle" in js
    assert "rich_textarea" in js
    assert "contenteditable" in js
    assert "iframe[id$=\"_ifr\"]" in js
    assert "fieldset-wrapper-content" in js
    assert "_hide" in js
    assert "a[href]" in js
    assert "query_action" in js
    assert "data-action" in js
    assert "filter[-_].*(apply|submit)" in js
    assert "effect_kind" not in js


def test_form_progress_fingerprint_excludes_transient_focus() -> None:
    assert "document.activeElement" not in _FORM_VALUE_FINGERPRINT_JS
    assert "e.value" in _FORM_VALUE_FINGERPRINT_JS
    assert "e.checked" in _FORM_VALUE_FINGERPRINT_JS


def test_form_controls_js_reads_below_fold_selects_and_multiselect():
    """WebArena 185 regression: <select> values must be captured below-fold (not gated
    on viewport) and expose the exact primary selected option."""
    js = form_controls_js()
    # below-fold selects/textareas captured for value-read (not viewport-gated)
    assert "keepForRead" in js
    assert "selectedOptions" in js
    assert "selectedOptions[0]" not in js
    assert "selected_text_primary" in js


def test_normalize_form_controls_keeps_multiselect_selected_text():
    controls = normalize_form_controls({
        "controls": [{
            "label": "Material",
            "kind": "native_select",
            "value": "33",
            "selected_text": "Cotton, Fleece",
            "selected_text_primary": "Cotton",
            "options": ["Burlap", "Canvas", "Cotton", "Fleece", "Wool"],
            "rect": {"x": 500.0, "y": 1800.0, "w": 200, "h": 80},
        }]
    })
    assert controls[0]["selected_text"] == "Cotton, Fleece"
    assert controls[0]["selected_text_primary"] == "Cotton"
    assert controls[0]["label"] == "Material"


def test_normalize_form_controls_keeps_native_select_options():
    controls = normalize_form_controls({
        "controls": [{
            "label": "Status",
            "kind": "native_select",
            "value": "",
            "selected_text": "",
            "options": ["", "Canceled", "Complete", "Processing"],
            "focused": True,
            "rect": {"x": 856.0, "y": 509.0, "w": 246, "h": 32},
        }]
    })

    assert controls == [{
        "kind": "native_select",
        "label": "Status",
        "selected_text": "",
        "options": ["Canceled", "Complete", "Processing"],
        "focused": True,
        "rect": {"x": 856, "y": 509, "w": 246, "h": 32},
    }]


def test_normalize_form_controls_keeps_structural_query_action():
    controls = normalize_form_controls({
        "controls": [{
            "label": "Localized submit label",
            "kind": "button",
            "query_action": "submit",
        }]
    })

    assert controls[0]["query_action"] == "submit"


def test_normalize_form_controls_keeps_section_toggle_affordance():
    controls = normalize_form_controls({
        "controls": [{
            "label": "Content",
            "kind": "section_toggle",
            "value": "false",
            "rect": {"x": 525.0, "y": 786.0, "w": 1000, "h": 40},
            "in_viewport": True,
            "viewport_pos": "in",
        }]
    })

    assert controls == [{
        "kind": "section_toggle",
        "label": "Content",
        "value": "false",
        "rect": {"x": 525, "y": 786, "w": 1000, "h": 40},
    }]


def test_normalize_form_controls_keeps_repeated_row_field_association():
    controls = normalize_form_controls({
        "controls": [
            {
                "label": "Swatch",
                "kind": "text_input",
                "name": "swatchtext[value][option_1][0]",
                "value": "",
                "required": True,
                "group_id": "manage-swatch:21",
                "group_index": 21,
                "group_field": "Admin",
            },
            {
                "label": "Default Store View",
                "kind": "text_input",
                "name": "optiontext[value][option_1][0]",
                "value": "XXXL",
                "group_id": "manage-swatch:21",
                "group_index": 21,
                "group_field": "Default Store View",
            },
        ]
    })

    assert controls[0]["group_id"] == controls[1]["group_id"]
    assert controls[0]["required"] is True
    assert controls[0]["group_field"] == "Admin"
    assert controls[1]["group_field"] == "Default Store View"

def test_normalize_form_controls_prioritizes_visible_rich_text_editor():
    raw_controls = [
        {
            "label": f"Offscreen {i}",
            "kind": "text_input",
            "rect": {"x": 500.0, "y": 1800.0, "w": 200, "h": 32},
            "in_viewport": False,
            "viewport_pos": "below",
        }
        for i in range(40)
    ]
    raw_controls.append({
        "label": "Short Description",
        "kind": "rich_textarea",
        "value": "",
        "rect": {"x": 530.0, "y": 760.0, "w": 540, "h": 260},
        "in_viewport": True,
        "viewport_pos": "in",
    })

    controls = normalize_form_controls({"controls": raw_controls})

    assert any(c["kind"] == "rich_textarea" and c["label"] == "Short Description" for c in controls)


def test_observation_accepts_optional_form_controls():
    obs = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[{"label": "Status", "kind": "native_select"}],
    )
    assert obs.form_controls == [{"label": "Status", "kind": "native_select"}]


def test_browser_perception_reads_form_controls(tmp_path):
    class _Client:
        viewport_size = (2048, 1152)

        def screenshot(self):
            return b"png"

        def is_loading(self):
            return False

        def page_info(self):
            return (
                "http://x/admin/orders",
                "Orders / Operations / Sales / Commerce Admin",
            )

        def form_state_fingerprint(self):
            return "abc123"

        def read_tables(self):
            return []

        def read_form_controls(self):
            return [{"label": "Status", "kind": "native_select"}]

        def read_form_controls_meta(self):
            return {
                "total_rendered": 1,
                "returned": 1,
                "truncated": False,
                "coverage": "complete",
                "raw_limit_hit": False,
            }

        def read_applied_filter_state(self):
            return {
                "Product": "Olivia",
            }, {
                "source": "adapter_state",
                "indicator_channel": "absent",
                "fallback_channel": "present",
            }

    client = _Client()
    obs = BrowserPerception(
        types.SimpleNamespace(client=client, screenshot=client.screenshot),
        tmp_path / "shot.png",
    ).observe()

    assert obs.form_controls == [{"label": "Status", "kind": "native_select"}]
    assert obs.form_controls_meta["coverage"] == "complete"
    assert obs.applied_filters == {"Product": "Olivia"}
    assert obs.applied_filter_state is not None
    assert obs.applied_filter_state.coverage == "complete"
    assert obs.applied_filter_state.predicates["product"].values == ["olivia"]
    assert obs.applied_filter_meta == {
        "source": "adapter_state",
        "indicator_channel": "absent",
        "fallback_channel": "present",
    }
    assert obs.title == "Orders / Operations / Sales / Commerce Admin"


def test_normalize_form_controls_reserves_slots_for_offscreen_controls():
    # Review F1: a large in-viewport set must not fully evict rendered-but-off-screen fields, or the
    # planner can't scroll to a target that scrolled off-screen. Off-viewport controls get a reserved
    # share of the cap (section_toggle/rich_textarea are priority 0/1 and kept regardless).
    from gui_agent.adapters.browser.form_reader import normalize_form_controls, MAX_CONTROLS
    raw = [
        {"kind": "input", "label": f"onscreen_{i}", "in_viewport": True,
         "rect": {"x": 0, "y": i, "w": 100, "h": 20}}
        for i in range(MAX_CONTROLS + 10)
    ]
    raw.append({"kind": "input", "label": "offscreen_target", "in_viewport": False,
                "viewport_pos": "below", "rect": {"x": 0, "y": 9999, "w": 100, "h": 20}})
    controls = normalize_form_controls({"controls": raw})
    assert len(controls) == MAX_CONTROLS
    assert "offscreen_target" in [c.get("label") for c in controls]


def test_snapshot_keeps_bottom_repeated_row_atomic_and_reports_truncation():
    raw: list[dict] = []
    for row in range(20):
        for field, value in (("Admin", ""), ("Label", f"value-{row}"), ("Store", "")):
            raw.append({
                "kind": "text_input",
                "label": field,
                "value": value,
                "group_id": f"collection:{row}",
                "group_index": row,
                "group_field": field,
                "in_viewport": row >= 17,
            })

    controls, meta = normalize_form_control_snapshot({
        "controls": raw,
        "total_rendered": len(raw),
        "raw_limit_hit": False,
    })

    bottom = [item for item in controls if item.get("group_id") == "collection:19"]
    assert {item.get("group_field") for item in bottom} == {"Admin", "Label", "Store"}
    from gui_agent.context.runtime import format_form_controls_text
    rendered = format_form_controls_text(controls, meta)
    assert "collection:19" in rendered
    assert 'field="Admin"' in rendered
    assert meta == {
        "total_rendered": 60,
        # The cap is not allowed to split a three-field row, so one slot remains unused.
        "returned": 39,
        "truncated": True,
        "coverage": "partial",
        "raw_limit_hit": False,
    }


def test_complete_control_state_is_retained_before_prompt_truncation():
    raw = {
        "controls": [
            {
                "kind": "text_input",
                "label": f"Field {index}",
                "name": f"field_{index}",
                "value": str(index),
                "in_viewport": True,
            }
            for index in range(45)
        ],
        "total_rendered": 45,
        "raw_limit_hit": False,
    }

    prompt_controls, prompt_meta = normalize_form_control_snapshot(raw)
    state_controls, state_meta = normalize_form_control_state(raw)

    assert len(prompt_controls) == 40
    assert prompt_meta["coverage"] == "partial"
    assert len(state_controls) == 45
    assert state_meta == {
        "total_rendered": 45,
        "returned": 45,
        "truncated": False,
        "coverage": "complete",
        "raw_limit_hit": False,
    }
