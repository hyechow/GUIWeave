from __future__ import annotations

import pytest

from gui_agent.core.tool_agent.action_guard import (
    assess_frame,
    assess_navigation_url,
)
from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.strategy import Strategy
from gui_agent.core.tool_agent.protocol import generic_action_spec


def _collector() -> WorkerSpec:
    return WorkerSpec.model_validate({
        "profile": "collector",
        "goal": "Collect the requested records",
        "success_criteria": ["Coverage is complete"],
        "data_requirements": [{
            "id": "records",
            "description": "Requested records",
            "row_schema": {"value": "string"},
        }],
        "strategy": {
            "approach": "Traverse the requested collection.",
        },
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


def test_frame_guard_preserves_runtime_capabilities() -> None:
    spec = _collector()
    frame = MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png")
    actions = [generic_action_spec("scroll"), generic_action_spec("tap")]

    assessment = assess_frame(spec, actions, frame)

    assert assessment.allowed_actions == actions


@pytest.mark.parametrize(
    ("outcome", "route"),
    [
        (WorkerOutcome(phase="failed", summary="blocked", failure_kind="worker_blocked", steps=1), "replace"),
        (WorkerOutcome(phase="failed", summary="blocked", failure_kind="navigation_blocked", steps=1), "replace"),
        (WorkerOutcome(phase="failed", summary="invalid", failure_kind="protocol_invalid", steps=1), "abort"),
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


def test_navigation_guard_allows_public_root_exact_or_same_origin() -> None:
    exact = "https://docs.example.test/reference?id=7"

    assert assess_navigation_url("https://search.example.test/").decision == "allow"
    assert assess_navigation_url(exact, authorized_urls={exact}).decision == "allow"
    assert assess_navigation_url(
        "https://active.example.test/next",
        current_url="https://active.example.test/current",
    ).decision == "allow"
    assert assess_navigation_url(
        "https://other.example.test/deep/path",
        current_url="https://active.example.test/current",
    ).decision == "abort"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://2130706433/admin",
    "http://169.254.169.254/metadata",
    "http://localhost/internal",
    "http://10.0.0.7/internal",
])
def test_navigation_guard_rejects_non_public_destinations(url: str) -> None:
    assert assess_navigation_url(url, authorized_urls={url}).decision == "abort"
