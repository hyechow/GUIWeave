from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control
from gui_agent.core.tool_agent.action_guard import WorkerActionCircuitBreaker
from gui_agent.core.tool_agent.contracts import MaterializedFrame


_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task108_date_to_grounding.json"
)
_SALES_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task108_sales_grounding.json"
)
_ORDERS_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "tool_agent"
    / "task108_orders_submenu_grounding.json"
)


def _field_failures(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for key, wanted in (expected.get("fields") or {}).items():
        value = actual.get(key)
        if value != wanted:
            failures.append(f"{key}: expected {wanted!r}, got {value!r}")
    for key, wanted in (expected.get("fields_casefold") or {}).items():
        value = actual.get(key)
        if str(value or "").casefold() != str(wanted).casefold():
            failures.append(
                f"{key}: expected case-insensitive {wanted!r}, got {value!r}"
            )
    if needle := expected.get("url_contains"):
        value = str(actual.get("url") or "")
        if str(needle) not in value:
            failures.append(f"url: expected to contain {needle!r}, got {value!r}")
    return failures


def score_action(action: Any, expected: dict[str, Any]) -> dict[str, Any]:
    """Score a replayed action without importing the removed protocol experiment."""

    action_type_correct = action.action_type == expected["action_type"]
    actual_fields = action.model_dump(mode="python")
    field_failures = _field_failures(actual_fields, expected)
    x = getattr(action, "x", None)
    y = getattr(action, "y", None)
    target_hit: bool | None = None
    if "target_box" in expected:
        left, right, top, bottom = map(float, expected["target_box"])
        target_hit = (
            x is not None
            and y is not None
            and left <= float(x) <= right
            and top <= float(y) <= bottom
        )
    distance = None
    if "point" in expected and x is not None and y is not None:
        point_x, point_y = map(float, expected["point"])
        distance = round(math.hypot(float(x) - point_x, float(y) - point_y), 3)
    return {
        "action_type_correct": action_type_correct,
        "target_hit": target_hit,
        "field_failures": field_failures,
        "fields_correct": not field_failures,
        "distance_to_recorded_point": distance,
        "ok": action_type_correct and not field_failures and target_hit is not False,
    }


def _case() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _frame(case: dict, *, scope_status: str = "unmet") -> MaterializedFrame:
    controls = [dict(item) for item in case["controls"]]
    return MaterializedFrame(
        frame_id=case["frame_id"],
        screenshot_path="task108-frame-8.png",
        url="http://webarena.test/admin/sales/order/",
        title="Orders",
        controls=controls,
        requirement_scopes={
            "completed_orders": {
                "status": scope_status,
                "applied_filters": {},
            },
        },
        missing_requirements=["completed_orders"],
    )


def test_task108_failed_type_point_replays_through_coordinate_grounding() -> None:
    case = _case()
    args = case["attempt"]["args"]
    original = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=args["x"],
        y=args["y"],
        text=args["text"],
        description=args["description"],
    ))

    grounded = ground_action_to_nearest_control(
        original,
        case["controls"],
        viewport_size=tuple(case["viewport_size"]),
    )

    assert score_action(original.action, case["expected_action"])["ok"] is False
    score = score_action(grounded.action, case["expected_action"])
    assert score["ok"] is True
    assert score["distance_to_recorded_point"] == 0.0
    assert grounded.action.snap == {
        "method": "control_geometry",
        "original": [207.0, 448.0],
        "snapped": [212.0, 428.0],
        "info": "to",
    }


def test_task108_sales_near_miss_replays_to_unique_sidebar_row() -> None:
    case = json.loads(_SALES_FIXTURE.read_text(encoding="utf-8"))
    args = case["attempt"]["args"]
    original = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=args["x"],
        y=args["y"],
        description=args["description"],
    ))

    grounded = ground_action_to_nearest_control(
        original,
        case["controls"],
        viewport_size=tuple(case["viewport_size"]),
    )

    assert score_action(original.action, case["expected_action"])["ok"] is False
    assert score_action(grounded.action, case["expected_action"])["ok"] is True
    assert (grounded.action.x, grounded.action.y) == (34.0, 174.0)
    assert grounded.action.snap["method"] == "control_geometry"
    assert grounded.action.snap["info"] == "SALES"


def test_task108_orders_near_miss_replays_through_semantic_disambiguation() -> None:
    case = json.loads(_ORDERS_FIXTURE.read_text(encoding="utf-8"))
    args = case["attempt"]["args"]
    original = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=args["x"],
        y=args["y"],
        description=args["description"],
    ))

    grounded = ground_action_to_nearest_control(
        original,
        case["controls"],
        viewport_size=tuple(case["viewport_size"]),
    )

    assert score_action(original.action, case["expected_action"])["ok"] is False
    score = score_action(grounded.action, case["expected_action"])
    assert score["ok"] is True
    assert score["distance_to_recorded_point"] == 0.0
    assert grounded.action.snap == {
        "method": "control_semantic_geometry",
        "original": [292.0, 140.0],
        "snapped": [173.0, 118.0],
        "info": "Orders",
    }


def test_task549_correct_row_link_point_is_not_snapped_to_filter_input() -> None:
    """Replay turn 19 where the model point was right but DOM assistance moved it."""

    original = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=109,
        y=438,
        description=(
            "Tap the 'size' text link in the Attribute Code column of the filtered "
            "grid row to open the edit form"
        ),
    ))
    controls = [{
        "kind": "text_input",
        "label": "Attribute Code",
        "group_field": "Attribute Code",
        "value": "size",
        "rect": {"x": 167, "y": 393, "w": 170, "h": 28},
    }]

    grounded = ground_action_to_nearest_control(
        original,
        controls,
        viewport_size=(1280, 963),
    )

    assert (grounded.action.x, grounded.action.y) == (109, 438)
    assert grounded.action.snap is None


def test_task549_wrong_filter_point_snaps_to_semantically_named_clickable_row() -> None:
    """Replay the 20260811 turn-7 miss before it consumes a replan."""

    original = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=167,
        y=392,
        description=(
            "Tap the row in the attributes grid where Attribute Code is 'size' "
            "to open its editor."
        ),
    ))
    controls = [
        {
            "kind": "text_input",
            "label": "Attribute Code",
            "value": "size",
            "rect": {"x": 167, "y": 392, "w": 170, "h": 28},
        },
        {
            "kind": "clickable_row",
            "row_values": ["size", "Size"],
            "rect": {"x": 550, "y": 438, "w": 1100, "h": 36},
        },
    ]

    grounded = ground_action_to_nearest_control(
        original,
        controls,
        viewport_size=(1280, 963),
    )

    assert (grounded.action.x, grounded.action.y) == (550, 438)
    assert grounded.action.snap == {
        "method": "control_semantic_geometry",
        "original": [167, 392],
        "snapped": [550.0, 438.0],
        "info": "clickable_row",
    }


def test_task549_wide_keyword_input_near_miss_snaps_to_unique_text_field() -> None:
    """Replay turn 15 where the point landed just right of a wide search input."""

    original = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=445,
        y=337,
        text="Minerva LumaTech V-Tee",
        description="Type into the Name keyword filter on the Products grid.",
    ))
    controls = [
        {
            "kind": "text_input",
            "label": "Search Search",
            "placeholder": "Search by keyword",
            "rect": {"x": 247, "y": 337, "w": 396, "h": 33},
        },
        {
            "kind": "number_input",
            "label": "of 11",
            "rect": {"x": 874, "y": 384, "w": 44, "h": 33},
        },
    ]

    grounded = ground_action_to_nearest_control(
        original,
        controls,
        viewport_size=(1280, 963),
    )

    assert (grounded.action.x, grounded.action.y) == (247.0, 337.0)
    assert grounded.action.snap == {
        "method": "control_geometry",
        "original": [445, 337],
        "snapped": [247.0, 337.0],
        "info": "Search Search",
    }


def test_task108_replay_blocks_third_equivalent_action_until_target_progresses() -> None:
    case = _case()
    attempt = case["attempt"]
    frame = _frame(case)
    breaker = WorkerActionCircuitBreaker()

    first = breaker.inspect(
        tool=attempt["tool"],
        capability=attempt["capability"],
        args=attempt["args"],
        frame=frame,
    )
    assert first.blocked is False
    breaker.record(first)
    second = breaker.inspect(
        tool=attempt["tool"],
        capability=attempt["capability"],
        args=attempt["args"],
        frame=frame,
    )
    assert second.blocked is False
    breaker.record(second)

    third = breaker.inspect(
        tool=attempt["tool"],
        capability=attempt["capability"],
        args=attempt["args"],
        frame=frame,
    )
    assert third.blocked is True
    assert third.prior_attempts == 2

    progressed = breaker.inspect(
        tool=attempt["tool"],
        capability=attempt["capability"],
        args=attempt["args"],
        frame=_frame(case, scope_status="met"),
    )
    assert progressed.blocked is False
    assert progressed.progress != third.progress


def test_action_alias_cannot_bypass_repeated_action_fuse() -> None:
    case = _case()
    attempt = case["attempt"]
    frame = _frame(case)
    breaker = WorkerActionCircuitBreaker()

    for tool in ("search_by_date", "search_by_date"):
        decision = breaker.inspect(
            tool=tool,
            capability=attempt["capability"],
            args=attempt["args"],
            frame=frame,
        )
        assert decision.blocked is False
        breaker.record(decision)

    aliased = breaker.inspect(
        tool="runtime_type_visible",
        capability=attempt["capability"],
        args=attempt["args"],
        frame=frame,
    )

    assert aliased.blocked is True
    assert aliased.prior_attempts == 2


def test_control_value_change_counts_as_task_progress() -> None:
    case = _case()
    attempt = case["attempt"]
    initial = _frame(case).model_copy(update={
        "controls": [{
            "kind": "rich_textarea",
            "label": "Description",
            "value": "old value",
            "focused": True,
        }],
    })
    cleared = initial.model_copy(update={
        "controls": [{
            "kind": "rich_textarea",
            "label": "Description",
            "value": "",
            "focused": True,
        }],
    })
    breaker = WorkerActionCircuitBreaker()

    for _ in range(2):
        decision = breaker.inspect(
            tool=attempt["tool"],
            capability=attempt["capability"],
            args=attempt["args"],
            frame=initial,
        )
        breaker.record(decision)

    progressed = breaker.inspect(
        tool=attempt["tool"],
        capability=attempt["capability"],
        args=attempt["args"],
        frame=cleared,
    )

    assert progressed.blocked is False


def test_visible_android_menu_controls_count_as_task_progress() -> None:
    case = _case()
    attempt = case["attempt"]
    closed = _frame(case).model_copy(update={"controls": []})
    opened = closed.model_copy(update={
        "controls": [{
            "kind": "button",
            "label": "Create New Channel",
            "value": "Create New Channel",
            "in_viewport": True,
            "rect": {"x": 500, "y": 900, "w": 1000, "h": 45},
        }],
    })
    breaker = WorkerActionCircuitBreaker()

    for _ in range(2):
        decision = breaker.inspect(
            tool=attempt["tool"],
            capability=attempt["capability"],
            args=attempt["args"],
            frame=closed,
        )
        breaker.record(decision)

    progressed = breaker.inspect(
        tool=attempt["tool"],
        capability=attempt["capability"],
        args=attempt["args"],
        frame=opened,
    )

    assert progressed.blocked is False


def test_action_guard_rejects_control_capability_mismatches() -> None:
    controls = [
        {
            "kind": "native_select",
            "label": "Material",
            "selected_text": "",
            "rect": {"x": 500, "y": 200, "w": 100, "h": 40},
        },
        {
            "kind": "button",
            "label": "Filters",
            "rect": {"x": 500, "y": 300, "w": 100, "h": 40},
        },
    ]
    frame = MaterializedFrame(
        frame_id="controls",
        screenshot_path="controls.png",
        controls=controls,
    )

    tap = WorkerActionCircuitBreaker().inspect(
        tool="tap", capability="tap", args={"x": 500, "y": 200}, frame=frame
    )
    typed = WorkerActionCircuitBreaker().inspect(
        tool="type", capability="type", args={"x": 500, "y": 300}, frame=frame
    )

    assert tap.blocked and "use select_option" in tap.reason
    assert typed.blocked and "editable input" in typed.reason


def test_action_guard_blocks_redundant_detail_scroll_and_unscoped_row() -> None:
    breaker = WorkerActionCircuitBreaker()
    detail = MaterializedFrame(
        frame_id="detail",
        screenshot_path="detail.png",
        requirement_scopes={"products": {
            "status": "unknown",
            "detail_resolution": {
                "detail_fields": ["material"],
                "current_observed_detail_fields": ["material"],
            },
        }},
    )
    assert breaker.inspect(
        tool="scroll",
        capability="scroll",
        args={"direction": "down", "description": "Reveal Material"},
        frame=detail,
    ).blocked

    row = detail.model_copy(update={
        "requirement_scopes": {"products": {"status": "unmet"}},
        "controls": [{
            "kind": "a",
            "label": "Edit product",
            "group_index": 0,
            "rect": {"x": 800, "y": 500, "w": 80, "h": 30},
        }],
    })
    assert breaker.inspect(
        tool="tap", capability="tap", args={"x": 800, "y": 500}, frame=row
    ).blocked
