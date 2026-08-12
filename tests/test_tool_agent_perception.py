from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from gui_agent.core.tool_agent.filter_state import (
    AppliedFilterState,
    compile_filter_predicates,
)
from gui_agent.core.tool_agent.contracts import DataRequirement
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.perception import PerceptionMaterializer


ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "term": {"type": "string"},
        "uses": {"type": "integer"},
    },
    "required": ["term", "uses"],
    "additionalProperties": False,
}
TASK185_FIXTURE = (
    Path(__file__).parent / "fixtures/tool_agent/task185_material_inheritance.json"
)


def _requirement() -> DataRequirement:
    return DataRequirement(
        id="terms",
        description="Most frequently used terms",
        target_label="Top Terms",
        row_schema=ROW_SCHEMA,
        field_sources={"term": "Search Term", "uses": "Uses"},
    )


class FakePlatform:
    def __init__(self) -> None:
        self.client = SimpleNamespace(page_info=lambda: ("https://example.test", "Example"))

    def screenshot(self) -> bytes:
        return b"png"


class FakePerception:
    def __init__(
        self,
        tables: list[dict],
        *,
        controls: list[dict] | None = None,
        control_state: list[dict] | None = None,
        applied_filters: dict[str, str] | None = None,
        applied_filter_state: AppliedFilterState | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.control_state = control_state
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state

    def observe(self):
        return SimpleNamespace(
            png_bytes=b"png",
            tables=self.tables,
            form_controls=self.controls,
            form_control_state=self.control_state,
            applied_filters=self.applied_filters,
            applied_filter_state=self.applied_filter_state,
            url="https://example.test",
            title="Example",
        )


class FakeBundle:
    def __init__(
        self,
        tables: list[dict],
        *,
        controls: list[dict] | None = None,
        control_state: list[dict] | None = None,
        applied_filters: dict[str, str] | None = None,
        applied_filter_state: AppliedFilterState | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.control_state = control_state
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state
        self.make_perception_calls = 0

    def make_perception(self, _platform, _path):
        self.make_perception_calls += 1
        return FakePerception(
            self.tables,
            controls=self.controls,
            control_state=self.control_state,
            applied_filters=self.applied_filters,
            applied_filter_state=self.applied_filter_state,
        )


def _materializer(tmp_path: Path, mode: str) -> PerceptionMaterializer:
    materializer = PerceptionMaterializer.__new__(PerceptionMaterializer)
    materializer.mode = mode
    materializer.data_store = RuntimeDataStore()
    materializer.log_dir = tmp_path
    materializer.model = "fake"
    materializer._expected_totals = {}
    materializer._detail_collections = {}
    materializer._vision_extract = lambda _requirement, _png, **_kwargs: {  # type: ignore[method-assign]
        "found": False,
        "rows": [],
        "end_visible": False,
    }
    return materializer


def test_enhanced_materializes_matching_structured_surface_outside_viewport(tmp_path: Path) -> None:
    tables = [
        {
            "caption": "Top Terms",
            "in_viewport": False,
            "viewport_pos": "below",
            "rows": [{"Search Term": "offscreen", "Uses": "99"}],
        },
        {
            "caption": "Top Terms",
            "in_viewport": True,
            "viewport_pos": "in",
            "rows": [{"Search Term": "visible", "Uses": "7"}],
        },
    ]
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("structured data must not require a visual gate")
    )

    frame, _ = materializer.observe(
        bundle=FakeBundle(tables),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert frame.chunks[0].provider == "structured"
    assert materializer.data_store.collection_chunks(frame.collections[0].ref) == [
        [{"term": "offscreen", "uses": 99}]
    ]
    assert frame.chunks[0].coverage["source_scope"] == "structured_surface"


def test_acquisition_scope_can_broaden_without_changing_logical_row_filter(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="reviews",
        description="Reviews for the requested exact product",
        row_schema={
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["product", "title"],
        },
        field_sources={"product": "Product", "title": "Title"},
        filters={"product": "Erica Sports Bra"},
    )
    tables = [{
        "caption": "Reviews",
        "headers": ["Product", "Title"],
        "rows": [
            {"Product": "Erica Sports Bra", "Title": "Target review"},
            {"Product": "Erica Training Top", "Title": "Related review"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static", "has_next_page": False},
    }]
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle(
            tables,
            applied_filters={"Product": "Erica"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({"Product": "Erica"}),
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        acquisition_filters={"product": "Erica"},
        frame_no=1,
    )

    assert frame.requirement_scopes["reviews"]["requested_filters"] == {
        "Product": "Erica"
    }
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [
        {"product": "Erica Sports Bra", "title": "Target review"},
        {"product": "Erica Training Top", "title": "Related review"},
    ]

    stale, _ = materializer.observe(
        bundle=FakeBundle(
            tables,
            applied_filters={"Product": "Erica Sports Bra"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({
                    "Product": "Erica Sports Bra"
                }),
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        acquisition_filters={},
        frame_no=2,
    )

    assert stale.requirement_scopes["reviews"]["status"] == "unmet"
    assert stale.requirement_scopes["reviews"]["evidence"] == (
        "unexpected_applied_filters"
    )
    assert stale.collections == []


def test_incomplete_visual_candidates_keep_detail_collection_open(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="review_details",
        description="Review title and detail rating",
        row_schema={
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "title": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["product", "title", "rating"],
        },
        field_sources={
            "product": "Product",
            "title": "Title",
            "rating": "Detailed Rating",
        },
        field_types={"product": "text", "title": "text", "rating": "number"},
        filters={"product": "Erica Sports Bra"},
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"product": "Erica Sports Bra", "title": "Candidate"}],
        "end_visible": True,
        "scope_satisfied": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.collections == []
    assert frame.missing_requirements == ["review_details"]


def test_visual_values_use_complete_enhanced_surface_as_coverage_evidence(
    tmp_path: Path,
) -> None:
    requirement = _requirement().model_copy(update={
        "target_label": "",
        "description": "Terms in the target order",
        "field_sources": {"term": "Term Name", "uses": "Uses"},
    })
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"term": "first", "uses": 4}, {"term": "second", "uses": 2}],
        "end_visible": False,
        "scope_satisfied": True,
    }
    distractor = {
        "caption": "Order Summary",
        "headers": ["Order Date", "Order Status"],
        "rows": [
            {"Order Date": str(index), "Order Status": "Complete"}
            for index in range(3)
        ],
        "partial": False,
        "in_viewport": True,
        "traversal": {"type": "static"},
    }
    table = {
        "caption": "Entries",
        "headers": ["Term", "Uses"],
        "rows": [{"Term": "first", "Uses": "4"}, {"Term": "second", "Uses": "2"}],
        "partial": False,
        "in_viewport": True,
        "traversal": {
            "type": "paged",
            "page_index": 1,
            "page_count": 1,
            "has_next_page": False,
        },
    }
    frame, _ = materializer.observe(
        bundle=FakeBundle([distractor, table]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.chunks[0].provider == "vision"
    assert frame.chunks[0].coverage["coverage_evidence"] == (
        "structured_surface_cardinality"
    )
    assert frame.chunks[0].coverage["end_visible"] is False
    assert frame.collections[0].row_count == 2
    assert frame.collections[0].coverage["status"] == "complete"


def test_detail_collection_keeps_candidate_total_and_survives_list_navigation(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="review_details",
        description="Review title and detail rating",
        row_schema={
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "title": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["product", "title", "rating"],
        },
        field_sources={
            "product": "Product",
            "title": "Title",
            "rating": "Detailed Rating",
        },
        field_types={"product": "text", "title": "text", "rating": "number"},
        filters={"product": "Requested Product"},
    )
    candidate_table = {
        "caption": "Reviews",
        "headers": ["Product", "Title", "Action"],
        "rows": [
            {"Product": "Candidate Product", "Title": "First", "Action": "Edit"},
            {"Product": "Candidate Product", "Title": "Second", "Action": "Edit"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static", "has_next_page": False},
    }
    extracts = iter([
        {
            "found": True,
            "rows": [{
                "product": "Candidate Product",
                "title": "First",
                "rating": 1,
            }],
            "end_visible": True,
            "scope_satisfied": True,
        },
        {
            "found": True,
            "rows": [{
                "product": "Candidate Product",
                "title": "Second",
                "rating": 4,
            }],
            "end_visible": True,
            "scope_satisfied": True,
        },
    ])
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: next(extracts)  # type: ignore[method-assign]
    scoped_list = FakeBundle(
        [candidate_table],
        applied_filters={"Product": "Candidate"},
        applied_filter_state=AppliedFilterState(
            predicates=compile_filter_predicates({"Product": "Candidate"}),
            coverage="complete",
            source="filter_indicator",
        ),
    )
    observe_args = {
        "platform": FakePlatform(),
        "requirements": [requirement],
        "acquisition_filters": {"product": "Candidate"},
    }

    initial, _ = materializer.observe(bundle=scoped_list, frame_no=1, **observe_args)
    first_detail, _ = materializer.observe(
        bundle=FakeBundle([]), frame_no=2, **observe_args
    )
    after_first, _ = materializer.observe(
        bundle=scoped_list, frame_no=3, **observe_args
    )
    second_detail, _ = materializer.observe(
        bundle=FakeBundle([]), frame_no=4, **observe_args
    )
    after_second, _ = materializer.observe(
        bundle=scoped_list, frame_no=5, **observe_args
    )

    assert initial.collections == []
    assert first_detail.collections[0].row_count == 1
    assert first_detail.collections[0].coverage["known_total"] == 2
    assert first_detail.collections[0].coverage["status"] == "incomplete"
    assert after_first.collections[0] == first_detail.collections[0]
    assert second_detail.collections[0].row_count == 2
    assert second_detail.collections[0].coverage["status"] == "complete"
    assert after_second.collections[0] == second_detail.collections[0]
    assert after_second.missing_requirements == []


def test_enhanced_prefers_complete_control_state_for_exact_grounding(tmp_path: Path) -> None:
    truncated = [{
        "kind": "text_input",
        "label": "search-global",
        "id": "search-global",
        "rect": {"x": 736, "y": 98, "w": 250, "h": 35},
    }]
    complete = [
        *truncated,
        {
            "kind": "text_input",
            "label": "to",
            "name": "created_at[to]",
            "id": "E1WHE5T",
            "rect": {"x": 212, "y": 428, "w": 184, "h": 32},
        },
    ]
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle([], controls=truncated, control_state=complete),
        platform=FakePlatform(),
        requirements=[],
        frame_no=1,
    )

    assert [control["id"] for control in frame.controls] == [
        "search-global",
        "E1WHE5T",
    ]
    assert frame.controls[1]["rect"] == {
        "x": 212,
        "y": 428,
        "w": 184,
        "h": 32,
    }


def test_enhanced_exposes_page_wide_surface_summary_without_row_values(tmp_path: Path) -> None:
    tables = [{
        "caption": "Results",
        "headers": ["Date", "Orders", "Total"],
        "rows": [
            {"Date": "private-date", "Orders": "3", "Total": "$12.00"},
            {"Date": "another-private-date", "Orders": "1", "Total": "$4.00"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static"},
    }]
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle(tables),
        platform=FakePlatform(),
        requirements=[],
        frame_no=1,
    )

    assert frame.structured_surfaces == [{
        "kind": "rendered_data_surface",
        "rendered": True,
        "caption": "Results",
        "fields": ["Date", "Orders", "Total"],
        "row_count": 2,
        "partial": False,
        "total_records": 2,
        "traversal": {"type": "static"},
    }]
    assert "private-date" not in frame.model_dump_json()


def test_enhanced_materializes_partial_dom_table_with_incomplete_coverage(tmp_path: Path) -> None:
    tables = [{
        "caption": "Top Terms",
        "in_viewport": True,
        "viewport_pos": "in",
        "rows": [{"Search Term": "offscreen-dom-row", "Uses": "99"}],
        "total_records": 10,
        "partial": True,
        "traversal": {
            "type": "paged",
            "page_index": 1,
            "page_count": 2,
            "has_next_page": True,
        },
    }]
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("partial structured rows must still be used")
    )

    frame, _ = materializer.observe(
        bundle=FakeBundle(tables),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert frame.chunks[0].provider == "structured"
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [
        {"term": "offscreen-dom-row", "uses": 99}
    ]
    assert frame.collections[0].coverage["status"] == "incomplete"
    assert frame.collections[0].coverage["known_total"] == 10
    assert frame.collections[0].coverage["pages_seen"] == [1]
    assert frame.collections[0].coverage["movement"] == {
        "type": "paged",
        "page_index": 1,
        "page_count": 2,
        "has_next_page": True,
    }


def test_enhanced_materializes_a_complete_empty_structured_collection(tmp_path: Path) -> None:
    tables = [{
        "caption": "Top Terms",
        "headers": ["Search Term", "Uses"],
        "rows": [],
        "total_records": 0,
        "partial": False,
        "traversal": {
            "type": "static",
            "has_next_page": False,
        },
    }]
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("an empty structured collection is still authoritative")
    )

    frame, _ = materializer.observe(
        bundle=FakeBundle(tables),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert frame.missing_requirements == []
    assert frame.chunks[0].provider == "structured"
    assert frame.collections[0].row_count == 0
    assert frame.collections[0].coverage["status"] == "complete"


def test_empty_surface_is_authoritative_when_detail_fields_are_not_grid_columns(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="review_details",
        description="Review details for one exact product query",
        row_schema={
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "title": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["product", "title", "rating"],
        },
        field_sources={
            "product": "Product",
            "title": "Title",
            "rating": "Detailed Rating",
        },
        field_types={
            "product": "text",
            "title": "text",
            "rating": "number",
        },
        filters={"product": "Exact Product Name"},
    )
    table = {
        "caption": "All Reviews",
        "headers": ["Product", "Title", "Action"],
        "rows": [],
        "total_records": 0,
        "partial": False,
        "traversal": {"type": "static", "has_next_page": False},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("the authoritative empty grid must not need detail fields")
    )

    frame, _ = materializer.observe(
        bundle=FakeBundle(
            [table],
            applied_filters={"Product": "Exact Product Name"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({"Product": "Exact Product Name"}),
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.missing_requirements == []
    assert frame.collections[0].row_count == 0
    assert frame.collections[0].coverage["scope_status"] == "met"
    assert frame.collections[0].coverage["status"] == "complete"


def test_collector_establishes_filter_scope_before_materializing_rows(tmp_path: Path) -> None:
    row_schema = {
        "type": "object",
        "properties": {
            "record_id": {"type": "string"},
            "owner": {"type": "string"},
            "status": {"type": "string"},
        },
        "required": ["record_id", "owner", "status"],
        "additionalProperties": False,
    }
    requirement = DataRequirement(
        id="filtered_records",
        description="Records in the required status",
        target_label="Records",
        row_schema=row_schema,
        field_sources={
            "record_id": "ID",
            "owner": "Owner",
            "status": "Status",
        },
        filters={"status": "Complete"},
    )
    unfiltered_table = {
        "caption": "Records",
        "headers": ["ID", "Owner", "Status"],
        "rows": [
            {"ID": "1", "Owner": "a", "Status": "Processing"},
            {"ID": "2", "Owner": "b", "Status": "Complete"},
        ],
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    controls = [{
        "kind": "native_select",
        "label": "Status",
        "options": ["Complete", "Processing"],
        "rect": {"x": 50, "y": 60, "w": 100, "h": 30},
    }]

    before, _ = materializer.observe(
        bundle=FakeBundle(
            [unfiltered_table],
            controls=controls,
            applied_filter_state=AppliedFilterState(
                predicates={},
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert before.collections == []
    assert before.missing_requirements == ["filtered_records"]
    assert before.requirement_scopes["filtered_records"]["status"] == "unmet"
    assert before.controls == [{
        "kind": "native_select",
        "label": "Status",
        "options": ["Complete", "Processing"],
        "rect": {"x": 50, "y": 60, "w": 100, "h": 30},
    }]

    filtered_table = {
        **unfiltered_table,
        "rows": [{"ID": "2", "Owner": "b", "Status": "Complete"}],
        "total_records": 1,
    }
    after, _ = materializer.observe(
        bundle=FakeBundle(
            [filtered_table],
            applied_filters={"Status": "Complete"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({"Status": "Complete"}),
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )

    assert after.requirement_scopes["filtered_records"]["status"] == "met"
    assert after.collections[0].coverage["scope_status"] == "met"
    assert materializer.data_store.collection_rows(after.collections[0].ref) == [{
        "record_id": "2",
        "owner": "b",
        "status": "Complete",
    }]


def test_linked_details_resolve_empty_values_from_related_records(tmp_path: Path) -> None:
    case = json.loads(TASK185_FIXTURE.read_text(encoding="utf-8"))
    requirement = DataRequirement.model_validate(case["requirement"])
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("enhanced list/detail evidence must not invoke visual extraction")
    )

    def observe(
        frame_no: int,
        *,
        table: dict | None = None,
        controls: list[dict] | None = None,
        applied: dict[str, str] | None = None,
        predicates: dict | None = None,
    ):
        frame, _ = materializer.observe(
            bundle=FakeBundle(
                [table] if table else [],
                controls=controls,
                applied_filters=applied,
                applied_filter_state=(
                    AppliedFilterState(
                        predicates=compile_filter_predicates(predicates),
                        coverage="complete",
                        source="replay",
                    )
                    if predicates is not None
                    else None
                ),
            ),
            platform=FakePlatform(),
            requirements=[requirement],
            acquisition_filters={"quantity": 3},
            frame_no=frame_no,
        )
        return frame

    initial = observe(
        1,
        table=case["list_table"],
        applied={"Quantity": "3 - 3"},
        predicates={"Quantity": 3},
    )
    assert initial.collections == []

    child = observe(2, controls=case["details"][0])
    detail = child.requirement_scopes[requirement.id]["detail_resolution"]
    assert detail["pending_candidate_ordinal"] == 1
    assert detail["current_observed_detail_fields"] == ["material"]

    parent_table = {
        "headers": ["Type", "SKU", "Action"],
        "rows": [{"Type": "Configurable Product", "SKU": "WH11", "Action": "Edit"}],
        "total_records": 1,
        "partial": False,
        "traversal": {"type": "static"},
    }
    parent_search = observe(
        3,
        table=parent_table,
        applied={
            "Keyword": "WH11",
            "Quantity": "3 - 3",
            "Type": "Configurable Product",
        },
        predicates={
            "Keyword": "WH11",
            "Quantity": 3,
            "Type": "Configurable Product",
        },
    )
    resolution = parent_search.requirement_scopes[requirement.id]["detail_resolution"]
    assert resolution["pending_candidate_ordinal"] == 1
    assert parent_search.requirement_scopes[requirement.id]["applied_filters"] == {
        "Keyword": "WH11",
        "Quantity": "3 - 3",
        "Type": "Configurable Product",
    }

    first_parent = observe(4, controls=case["details"][1])
    progress = first_parent.requirement_scopes[requirement.id]["detail_resolution"]
    assert progress["resolved_candidate_ordinals"] == [1]
    assert progress["next_unresolved_candidate"]["ordinal"] == 2

    observe(5, controls=case["details"][2])
    observe(6, controls=case["details"][3])
    broad = observe(
        7,
        table=case["list_table"],
        applied={"Quantity": "3 - ..."},
        predicates={"Quantity": {"from": 3}},
    )
    assert broad.collections == []
    assert broad.requirement_scopes[requirement.id]["detail_resolution"]["status"] == "resolved"

    completed = observe(
        8,
        table=case["list_table"],
        applied={"Quantity": "3 - 3"},
        predicates={"Quantity": 3},
    )
    collection = completed.collections[0]
    assert collection.coverage["status"] == "complete"
    assert collection.row_count == 2
    assert completed.chunks[0].provider == "structured"
    assert materializer.data_store.collection_rows(collection.ref) == case["expected_rows"]


def test_vision_only_never_invokes_platform_perception(tmp_path: Path) -> None:
    bundle = FakeBundle([
        {
            "caption": "Top Terms",
            "in_viewport": True,
            "rows": [{"Search Term": "hidden-dom-value", "Uses": "99"}],
        }
    ])
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda _requirement, _png, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"term": "visual-value", "uses": 2}],
        "end_visible": True,
    }

    frame, _ = materializer.observe(
        bundle=bundle,
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert bundle.make_perception_calls == 0
    assert frame.chunks[0].provider == "vision"
    assert materializer.data_store.collection_chunks(frame.collections[0].ref) == [
        [{"term": "visual-value", "uses": 2}]
    ]


def test_vision_only_normalizes_declared_datetime_before_storage(tmp_path: Path) -> None:
    requirement = DataRequirement(
        id="orders",
        description="Visible orders",
        row_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "purchase_date": {"type": "string"},
            },
            "required": ["id", "purchase_date"],
        },
        field_sources={"id": "ID", "purchase_date": "Purchase Date"},
        field_types={"id": "text", "purchase_date": "datetime"},
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda _requirement, _png, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"id": "1", "purchase_date": "May 19, 2023 8:11:51 AM"}],
        "end_visible": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.chunks[0].provider == "vision"
    assert frame.chunks[0].coverage["normalization"] == {
        "id": "text",
        "purchase_date": "datetime",
    }
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [{
        "id": "1",
        "purchase_date": "2023-05-19T08:11:51+00:00",
    }]
