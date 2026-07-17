from __future__ import annotations

from gui_agent.adapters.browser.semantic_page import _walk, build_semantic_tree
from gui_agent.adapters.browser.target_binding import (
    active_surface_id,
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


def test_semantic_tree_attaches_visible_point_from_layout_snapshot():
    responses = {
        "Accessibility.getFullAXTree": {
            "nodes": [
                {
                    "nodeId": "root",
                    "role": {"value": "RootWebArea"},
                    "childIds": ["search"],
                },
                {
                    "nodeId": "search",
                    "role": {"value": "button"},
                    "name": {"value": "Search"},
                    "backendDOMNodeId": 18,
                },
            ],
        },
        "DOMSnapshot.captureSnapshot": {
            "documents": [{
                "nodes": {"backendNodeId": [18]},
                "layout": {"nodeIndex": [0], "bounds": [[100, 200, 40, 20]]},
            }],
        },
        "Page.getLayoutMetrics": {
            "cssVisualViewport": {
                "pageX": 0,
                "pageY": 100,
                "clientWidth": 1000,
                "clientHeight": 500,
            },
        },
    }

    result = build_semantic_tree(lambda method, _params: responses[method])

    assert result == [{
        "role": "button",
        "key": "Search",
        "value": "",
        "url": "",
        "ref": 18,
        "depth": 0,
        "in_viewport": True,
        "point": {"x": 120.0, "y": 220.0},
    }]


def test_semantic_tree_keeps_offscreen_point_for_exact_transport_direction():
    responses = {
        "Accessibility.getFullAXTree": {
            "nodes": [
                {
                    "nodeId": "root",
                    "role": {"value": "RootWebArea"},
                    "childIds": ["target"],
                },
                {
                    "nodeId": "target",
                    "role": {"value": "button"},
                    "name": {"value": "Add Swatch"},
                    "backendDOMNodeId": 19,
                },
            ],
        },
        "DOMSnapshot.captureSnapshot": {
            "documents": [{
                "nodes": {"backendNodeId": [19]},
                "layout": {"nodeIndex": [0], "bounds": [[100, 900, 40, 20]]},
            }],
        },
        "Page.getLayoutMetrics": {
            "cssVisualViewport": {
                "pageX": 0,
                "pageY": 100,
                "clientWidth": 1000,
                "clientHeight": 500,
            },
        },
    }

    result = build_semantic_tree(lambda method, _params: responses[method])

    assert result[0]["in_viewport"] is False
    assert result[0]["point"] == {"x": 120.0, "y": 1620.0}


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
