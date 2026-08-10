from __future__ import annotations

import json
from pathlib import Path

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control
from gui_agent.core.tool_agent.action_guard import WorkerActionCircuitBreaker
from gui_agent.core.tool_agent.contracts import MaterializedFrame
from manager_protocol.state_action_run import score_action


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
