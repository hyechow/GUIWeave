from __future__ import annotations

import pytest

from gui_agent.core.tool_agent.action_guard import (
    WorkerActionCircuitBreaker,
    assess_frame,
    assess_navigation_url,
)
from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    DataChunkRef,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.strategy import Strategy
from gui_agent.core.tool_agent.protocol import generic_action_spec


def _collector(*, cardinality: str = "many", coverage: str = "complete") -> WorkerSpec:
    return WorkerSpec.model_validate({
        "profile": "collector",
        "goal": "Collect the requested records",
        "success_criteria": ["Coverage is complete"],
        "data_requirements": [{
            "id": "records",
            "description": "Requested records",
            "cardinality": cardinality,
            "coverage": coverage,
            "row_schema": {"value": "string"},
        }],
        "strategy": {
            "approach": "Traverse the requested collection.",
        },
    })


def _first_match_collector() -> WorkerSpec:
    return WorkerSpec.model_validate({
        "profile": "collector",
        "goal": "Find the least expensive matching record",
        "success_criteria": ["Return the first match in ascending price order"],
        "data_requirements": [{
            "id": "records",
            "description": "Matching records",
            "cardinality": "one",
            "coverage": "first_match",
            "row_schema": {"value": "string"},
        }],
        "strategy": {"approach": "Sort by price ascending, then take the first match."},
    })


def _collection(*, rows: int = 1, filtered: bool = False) -> CollectionRef:
    spec = _collector()
    return CollectionRef(
        ref="collection:records",
        requirement_id="records",
        chunk_refs=["chunk:records:1"],
        row_count=rows,
        row_schema=spec.data_requirements[0].row_schema,
        coverage={
            "scope_status": "met",
            "status": "complete",
            "requested_filters": {"query": "exact"} if filtered else {},
            "applied_filters": {"query": "exact"} if filtered else {},
        },
    )


def test_action_guard_owns_collector_readiness() -> None:
    spec = _collector()
    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        collections=[_collection()],
    )

    assessment = assess_frame(spec, [generic_action_spec("scroll")], frame)

    assert assessment.ready_collection is not None
    assert assessment.completion_mode == "collector"


def test_singleton_first_match_can_complete_from_retained_collection() -> None:
    spec = _collector(cardinality="one", coverage="first_match")
    collection = _collection().model_copy(update={
        "coverage": {
            **_collection().coverage,
            "requested": "first_match",
            "cardinality": "one",
        },
    })
    frame = MaterializedFrame(
        frame_id="frame:2",
        screenshot_path="frame.png",
        collections=[collection],
    )

    assessment = assess_frame(spec, [generic_action_spec("scroll")], frame)

    assert assessment.ready_collection is not None
    assert assessment.completion_mode == "collector"
    assert assessment.allowed_actions == []


def test_singleton_first_match_accepts_completed_linked_detail_evidence() -> None:
    spec = _collector(cardinality="one", coverage="first_match")
    collection = _collection().model_copy(update={
        "coverage": {
            **_collection().coverage,
            "requested": "first_match",
            "cardinality": "one",
            "scope_status": "unknown",
            "coverage_evidence": "linked_detail_assembly",
        },
    })
    frame = MaterializedFrame(
        frame_id="frame:detail",
        screenshot_path="frame.png",
        collections=[collection],
    )

    assessment = assess_frame(spec, [generic_action_spec("scroll")], frame)

    assert assessment.allowed_actions == []
    assert assessment.completion_mode == "collector"


def test_frame_guard_preserves_capabilities_until_an_unready_attempt() -> None:
    spec = _collector()
    frame = MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png")
    actions = [generic_action_spec("scroll"), generic_action_spec("open_url")]

    ready = assess_frame(spec, actions, frame)
    unready_frame = frame.model_copy(update={
        "readiness": "loading",
        "readiness_reason": "main content has not materialized",
    })
    initial = assess_frame(spec, actions, unready_frame)
    attempted = assess_frame(spec, actions, unready_frame, attempted_action=True)

    assert ready.allowed_actions == actions
    assert [action.capability for action in initial.allowed_actions] == ["open_url"]
    assert attempted.allowed_actions == []
    assert attempted.completion_mode == "unavailable"


def test_complete_structured_page_exposes_authoritative_next_window_action() -> None:
    spec = _collector()
    collection = _collection().model_copy(update={
        "coverage": {**_collection().coverage, "status": "incomplete"},
    })
    frame = MaterializedFrame(
        frame_id="frame:page1",
        screenshot_path="frame.png",
        collections=[collection],
        chunks=[DataChunkRef(
            ref="chunk:records:1",
            requirement_id="records",
            frame_id="frame:page1",
            provider="structured",
            row_count=36,
            row_schema=spec.data_requirements[0].row_schema,
            coverage={
                "partial": False,
                "has_next_page": True,
                "movement": {
                    "next_url": "https://example.test/records?p=2",
                },
            },
        )],
    )
    assessment = assess_frame(
        spec,
        [generic_action_spec("scroll"), generic_action_spec("reveal_control"),
         generic_action_spec("open_url")],
        frame,
    )

    assert [action.capability for action in assessment.allowed_actions] == ["open_url"]
    assert assessment.allowed_actions[0].fixed_args["url"] == (
        "https://example.test/records?p=2"
    )
    assert "url" not in assessment.allowed_actions[0].exposed_args


def test_no_match_on_complete_structured_page_advances_without_collection() -> None:
    spec = _collector()
    frame = MaterializedFrame(
        frame_id="frame:no-match",
        screenshot_path="frame.png",
        requirement_scopes={
            "records": {"query_outcome": "no_matching_rows_on_complete_page"},
        },
        structured_surfaces=[{
            "kind": "rendered_data_surface",
            "partial": False,
            "traversal": {
                "type": "paged",
                "has_next_page": True,
                "next_url": "https://example.test/records?p=2",
            },
        }],
    )

    assessment = assess_frame(
        spec,
        [generic_action_spec("scroll"), generic_action_spec("tap"),
         generic_action_spec("open_url")],
        frame,
    )

    assert [action.capability for action in assessment.allowed_actions] == ["open_url"]
    assert assessment.allowed_actions[0].fixed_args["url"].endswith("p=2")


def test_singleton_first_match_does_not_follow_unaligned_surface_pagination() -> None:
    spec = _first_match_collector()
    frame = MaterializedFrame(
        frame_id="frame:homepage",
        screenshot_path="frame.png",
        requirement_scopes={"records": {
            "status": "unknown",
            "query_outcome": "no_matching_rows_on_complete_page",
        }},
        chunks=[DataChunkRef(
            ref="chunk:records:homepage",
            requirement_id="records",
            frame_id="frame:homepage",
            provider="structured",
            row_count=12,
            row_schema=spec.data_requirements[0].row_schema,
            coverage={
                "partial": False,
                "has_next_page": True,
                "movement": {"next_url": "https://example.test/?showcase=2"},
            },
        )],
    )
    actions = [generic_action_spec("tap"), generic_action_spec("open_url")]

    assessment = assess_frame(spec, actions, frame)

    assert assessment.allowed_actions == actions


def test_singleton_first_match_follows_next_page_after_ordered_no_match() -> None:
    spec = _first_match_collector()
    frame = MaterializedFrame(
        frame_id="frame:ordered-page",
        screenshot_path="frame.png",
        controls=[{
            "label": "Sort By",
            "selected_text_primary": "Price",
            "options": ["Price"],
        }],
        requirement_scopes={"records": {
            "query_outcome": "no_matching_rows_on_complete_page",
        }},
        structured_surfaces=[{
            "kind": "rendered_data_surface",
            "partial": False,
            "traversal": {
                "type": "paged",
                "has_next_page": True,
                "next_url": "https://example.test/products?p=2",
            },
        }],
    )

    assessment = assess_frame(
        spec,
        [generic_action_spec("tap"), generic_action_spec("open_url")],
        frame,
    )

    assert [action.capability for action in assessment.allowed_actions] == ["open_url"]
    assert assessment.allowed_actions[0].fixed_args["url"].endswith("p=2")


def test_structured_pagination_maximizes_page_size_before_advancing() -> None:
    spec = _collector()
    frame = MaterializedFrame(
        frame_id="frame:small-window",
        screenshot_path="frame.png",
        controls=[{
            "kind": "native_select",
            "label": "Show",
            "value": "12",
            "selected_text_primary": "12",
            "options": ["12", "24", "36"],
            "is_filter": True,
            "rect": {"x": 900, "y": 2698, "w": 58, "h": 32},
            "in_viewport": False,
        }],
        chunks=[DataChunkRef(
            ref="chunk:records:small-window",
            requirement_id="records",
            frame_id="frame:small-window",
            provider="structured",
            row_count=12,
            row_schema=spec.data_requirements[0].row_schema,
            coverage={
                "partial": False,
                "has_next_page": True,
                "movement": {"next_url": "https://example.test/records?p=2"},
            },
        )],
    )

    assessment = assess_frame(
        spec,
        [generic_action_spec("select_option"), generic_action_spec("open_url")],
        frame,
    )

    assert [action.capability for action in assessment.allowed_actions] == ["select_option"]
    assert assessment.allowed_actions[0].fixed_args == {
        "x": 900,
        "y": 2698,
        "text": "36",
    }


@pytest.mark.parametrize(
    ("outcome", "route"),
    [
        (WorkerOutcome(phase="failed", summary="blocked", failure_kind="worker_blocked", steps=1), "replace"),
        (WorkerOutcome(phase="failed", summary="blocked", failure_kind="navigation_blocked", steps=1), "replace"),
        (WorkerOutcome(phase="failed", summary="invalid", failure_kind="protocol_invalid", steps=1), "abort"),
        (WorkerOutcome(phase="failed", summary="unsupported", failure_kind="action_not_allowed", steps=1), "abort"),
        (WorkerOutcome(phase="completed", summary="done", steps=1), "complete"),
    ],
)
def test_strategy_routes_worker_outcomes(outcome: WorkerOutcome, route: str) -> None:
    assert Strategy.route(outcome) == route


def test_authoritative_empty_is_a_completed_immutable_data_contract() -> None:
    filtered = WorkerOutcome(
        phase="completed",
        summary="empty",
        collection_ref=_collection(rows=0, filtered=True),
        steps=0,
    )
    unfiltered = filtered.model_copy(update={
        "collection_ref": _collection(rows=0),
    })

    assert Strategy.route(filtered) == "complete"
    assert Strategy.route(unfiltered) == "complete"


def test_navigation_guard_allows_any_public_http_destination() -> None:
    assert assess_navigation_url("https://search.example.test/").decision == "allow"
    assert assess_navigation_url(
        "https://docs.example.test/reference?id=7",
    ).decision == "allow"
    assert assess_navigation_url(
        "https://other.example.test/deep/path",
    ).decision == "allow"


def test_current_frame_inventory_blocks_historical_tap_geometry() -> None:
    frame = MaterializedFrame(
        frame_id="frame:50",
        screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Add to Cart",
            "rect": {"x": 703, "y": 499, "w": 137, "h": 52},
        }],
    )

    decision = WorkerActionCircuitBreaker().inspect(
        tool="tap",
        capability="tap",
        args={"x": 628, "y": 15, "description": "Historical Size option"},
        frame=frame,
    )

    assert decision.blocked is True
    assert "historical or inferred geometry" in decision.reason


def test_reveal_of_now_visible_control_returns_current_state_feedback() -> None:
    frame = MaterializedFrame(
        frame_id="frame:31",
        screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Add to Cart",
            "rect": {"x": 703, "y": 501, "w": 137, "h": 52},
        }],
    )

    decision = WorkerActionCircuitBreaker().inspect(
        tool="reveal_control",
        capability="reveal_control",
        args={"x": 703, "y": 501, "description": "Add to Cart"},
        frame=frame,
    )

    assert decision.blocked is True
    assert "already visible in the current frame" in decision.reason


def test_reveal_requires_current_offscreen_control_geometry() -> None:
    frame = MaterializedFrame(
        frame_id="frame:29",
        screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Add to Cart", "in_viewport": False,
            "rect": {"x": 703, "y": 2219, "w": 137, "h": 52},
        }],
    )

    allowed = WorkerActionCircuitBreaker().inspect(
        tool="reveal_control",
        capability="reveal_control",
        args={"x": 703, "y": 2219, "description": "Add to Cart"},
        frame=frame,
    )

    assert allowed.blocked is False


def test_select_option_can_target_current_offscreen_choice_atomically() -> None:
    frame = MaterializedFrame(
        frame_id="frame:choice",
        screenshot_path="frame.png",
        controls=[{
            "kind": "native_select",
            "label": "Show",
            "id": "limiter",
            "options": ["12", "24", "36"],
            "in_viewport": False,
            "rect": {"x": 900, "y": 2698, "w": 58, "h": 32},
        }],
    )

    decision = WorkerActionCircuitBreaker().inspect(
        tool="select_option",
        capability="select_option",
        args={
            "x": 900, "y": 2698, "text": "36",
            "description": "Show choice control below the fold",
        },
        frame=frame,
    )

    assert decision.blocked is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://2130706433/admin",
    "http://169.254.169.254/metadata",
    "http://localhost/internal",
    "http://10.0.0.7/internal",
])
def test_navigation_guard_rejects_non_public_destinations(url: str) -> None:
    assert assess_navigation_url(url).decision == "abort"
