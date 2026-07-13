from __future__ import annotations

from gui_agent.adapters.browser.semantic_page import _walk
from gui_agent.adapters.browser.target_binding import (
    active_surface_id,
    active_target_aliases,
)
from gui_agent.core.schemas import Observation


def test_semantic_tree_excludes_ignored_interactive_nodes():
    nodes = {
        "root": {
            "nodeId": "root",
            "role": {"value": "RootWebArea"},
            "childIds": ["hidden", "visible"],
        },
        "hidden": {
            "nodeId": "hidden",
            "role": {"value": "button"},
            "name": {"value": "Background entry"},
            "backendDOMNodeId": 17,
            "ignored": True,
        },
        "visible": {
            "nodeId": "visible",
            "role": {"value": "button"},
            "name": {"value": "Active control"},
            "backendDOMNodeId": 18,
            "ignored": False,
        },
    }
    result: list[dict] = []

    _walk("root", nodes, 0, result)

    assert [item["key"] for item in result] == ["Active control"]


def test_browser_adapter_translates_dom_and_ax_targets_to_neutral_aliases():
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[{"label": "Field label", "name": "field_name"}],
        semantic_tree=[{"role": "button", "key": "Action label", "ref": 18, "depth": 0}],
    )

    assert active_target_aliases(observation) == {
        "Field label",
        "field_name",
        "Action label",
    }


def test_dialog_surface_excludes_background_targets_and_tracks_stage_heading():
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        url="https://example.test/editor/7",
        form_controls=[{"label": "Background field"}],
        semantic_tree=[
            {"role": "button", "key": "Background action", "ref": 10, "depth": 0},
            {"role": "dialog", "key": "Configure", "ref": 20, "depth": 0},
            {"role": "heading", "key": "Configure", "ref": 21, "depth": 1},
            {"role": "button", "key": "Next", "ref": 22, "depth": 1},
            {"role": "heading", "key": "Step 2", "ref": 23, "depth": 1},
        ],
    )

    assert active_surface_id(observation) == "dialog:20:Step 2"
    assert active_target_aliases(observation) == {"Configure", "Next", "Step 2"}
