from __future__ import annotations

import types

from gui_agent.adapters.browser.form_reader import (
    form_controls_js,
    normalize_form_controls,
)
from gui_agent.adapters.browser.perception import BrowserPerception
from gui_agent.core.schemas import Observation


def test_form_controls_js_is_serialized_expression():
    js = form_controls_js()
    assert "querySelectorAll" in js
    assert "input,select,textarea" in js
    assert "native_select" in js


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
        "options": ["Canceled", "Complete", "Processing"],
        "focused": True,
        "rect": {"x": 856, "y": 509, "w": 246, "h": 32},
    }]


def test_observation_accepts_optional_form_controls():
    obs = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[{"label": "Status", "kind": "native_select"}],
    )
    assert obs.form_controls == [{"label": "Status", "kind": "native_select"}]


def test_browser_perception_reads_form_controls(tmp_path):
    class _Client:
        def screenshot(self):
            return b"png"

        def is_loading(self):
            return False

        def page_info(self):
            return "http://x/admin/orders", "Orders"

        def form_state_fingerprint(self):
            return "abc123"

        def read_tables(self):
            return []

        def read_form_controls(self):
            return [{"label": "Status", "kind": "native_select"}]

    client = _Client()
    obs = BrowserPerception(
        types.SimpleNamespace(client=client, screenshot=client.screenshot),
        tmp_path / "shot.png",
    ).observe()

    assert obs.form_controls == [{"label": "Status", "kind": "native_select"}]
    assert obs.title == "Orders"
