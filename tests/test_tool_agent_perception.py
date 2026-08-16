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
from gui_agent.core.tool_agent.perception import (
    PerceptionMaterializer,
    derive_required_interactions,
    _visible_row_schema,
    _rows_satisfy_filters,
)


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


def test_perception_derives_unique_required_interaction() -> None:
    opener = {
        "kind": "button", "label": "Search", "query_action": "open",
        "enabled": True, "in_viewport": True,
        "action_point": {"x": 800, "y": 80},
    }
    pending = derive_required_interactions(
        [opener], {}, pending_capabilities={"type"},
    )

    assert len(pending) == 1
    assert pending[0].capability == "tap"
    assert pending[0].args["x"] == 800.0
    assert derive_required_interactions([opener], {}) == []
    scopes = {"rows": {"detail_resolution": {
        "status": "active", "next_unresolved_candidate": {"ordinal": 2},
    }}}
    assert len(derive_required_interactions([opener], scopes)) == 1
    assert derive_required_interactions(
        [opener, {
            "kind": "text_input", "is_filter": True,
            "enabled": True, "in_viewport": True,
        }],
        scopes,
    ) == []


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
        collection_regions: list | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.control_state = control_state
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state
        self.collection_regions = collection_regions

    def observe(self):
        return SimpleNamespace(
            png_bytes=b"png",
            tables=self.tables,
            form_controls=self.controls,
            form_control_state=self.control_state,
            applied_filters=self.applied_filters,
            applied_filter_state=self.applied_filter_state,
            collection_regions=self.collection_regions,
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
        collection_regions: list | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.control_state = control_state
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state
        self.collection_regions = collection_regions
        self.make_perception_calls = 0

    def make_perception(self, _platform, _path):
        self.make_perception_calls += 1
        return FakePerception(
            self.tables,
            controls=self.controls,
            control_state=self.control_state,
            applied_filters=self.applied_filters,
            applied_filter_state=self.applied_filter_state,
            collection_regions=self.collection_regions,
        )


def _materializer(tmp_path: Path, mode: str) -> PerceptionMaterializer:
    materializer = PerceptionMaterializer.__new__(PerceptionMaterializer)
    materializer.mode = mode
    materializer.data_store = RuntimeDataStore()
    materializer.log_dir = tmp_path
    materializer.model = "fake"
    materializer._expected_totals = {}
    materializer._detail_collections = {}
    materializer._visual_filter_states = {}
    materializer._vision_extract = lambda _requirement, _png, **_kwargs: {  # type: ignore[method-assign]
        "found": False,
        "rows": [],
        "end_visible": False,
    }
    return materializer


def _observe_requirement(
    materializer: PerceptionMaterializer,
    requirement: DataRequirement,
    frame_no: int,
    *,
    tables: list[dict] | None = None,
    acquisition_filters: dict | None = None,
    allow_linked_details: bool = True,
):
    return materializer.observe(
        bundle=FakeBundle(tables or []),
        platform=FakePlatform(),
        requirements=[requirement],
        acquisition_filters=acquisition_filters,
        allow_linked_details=allow_linked_details,
        frame_no=frame_no,
    )[0]


def _visible_region() -> SimpleNamespace:
    return SimpleNamespace(
        caption="Saved items",
        bounds=None,
        traversal={"type": "scroll"},
        cells=[SimpleNamespace(
            ref="android:1.2",
            bounds=(10.0, 20.0, 900.0, 500.0),
            texts=["@demo", "Exact visible text 💛🐾"],
            clipped_top=False,
            clipped_bottom=False,
        )],
    )


def test_operator_projects_exact_visible_collection_cell_text(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle([], collection_regions=[_visible_region()]),
        platform=FakePlatform(),
        requirements=[],
        frame_no=1,
    )

    assert len(frame.visible_collection_regions) == 1
    region = frame.visible_collection_regions[0]
    assert region["caption"] == "Saved items"
    assert region["traversal"] == {"type": "scroll"}
    assert region["viewport_tail_clipped"] is False
    assert region["cells"][0]["texts"] == ["@demo", "Exact visible text 💛🐾"]


def test_collector_does_not_project_collection_cell_values(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle(
            [{
                "caption": "Top Terms",
                "headers": ["Search Term", "Uses"],
                "rows": [{"Search Term": "private", "Uses": 1}],
            }],
            collection_regions=[_visible_region()],
        ),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert frame.visible_collection_regions == []


def test_operator_marks_a_clipped_collection_tail(tmp_path: Path) -> None:
    region = _visible_region()
    region.cells[-1].clipped_bottom = True
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle([], collection_regions=[region]),
        platform=FakePlatform(),
        requirements=[],
        frame_no=1,
    )

    assert frame.visible_collection_regions[0]["viewport_tail_clipped"] is True


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


def test_structured_surface_resolves_caption_qualified_field(tmp_path: Path) -> None:
    requirement = DataRequirement(
        id="orders",
        description="All order records",
        row_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        field_sources={"order_id": "Order ID"},
        field_types={"order_id": "text"},
    )
    tables = [
        {"caption": "Customer List", "headers": ["ID"], "rows": [{"ID": "99"}]},
        {
            "caption": "Order List",
            "headers": ["ID"],
            "rows": [{"ID": "32"}],
            "partial": False,
            "traversal": {"type": "scroll", "at_scroll_end": True},
        },
    ]
    materializer = _materializer(tmp_path, "enhanced")

    frame, _ = materializer.observe(
        bundle=FakeBundle(tables),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.chunks[0].provider == "structured"
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [
        {"order_id": "32"}
    ]
    assert frame.collections[0].coverage["status"] == "complete"


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
    ]
    assert frame.collections[0].coverage["status"] == "complete"

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


def test_visual_candidate_must_satisfy_immutable_logical_filter(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="target_record",
        description="One exact requested record",
        row_schema={"date": "string"},
        filters={"date": "2026-08-15"},
        coverage="first_match",
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"date": "2026-08-27"}],
        "end_visible": False,
        "filter_state_visible": True,
        "scope_satisfied": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        acquisition_filters={"date": "2026-08"},
        frame_no=1,
    )

    scope = frame.requirement_scopes[requirement.id]
    assert scope["status"] == "met"
    assert scope["filter_rejected_rows"] == 1
    assert frame.collections == []
    assert frame.missing_requirements == [requirement.id]


def test_known_filter_mismatch_wins_over_unknown_row() -> None:
    requirement = DataRequirement(
        id="records",
        description="Filtered records",
        row_schema={"date": "string", "content": "string"},
        filters={"date": "2026-08-15"},
    )

    assert _rows_satisfy_filters(requirement, [
        {"content": "Date not visible"},
        {"date": "2026-08-16", "content": "Wrong date"},
    ]) is False


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
    assert frame.requirement_scopes["review_details"]["schema_rejected_rows"] == 1
    assert frame.requirement_scopes["review_details"]["collection_blockers"] == [
        "visible rows did not satisfy the required row schema"
    ]


def test_vision_extract_preserves_schema_rejected_rows_for_empty_classification(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="forecast",
        description="Requested forecast fields",
        row_schema={
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "temperature": {"type": "number"},
                "optional_source_metric": {"type": "number"},
            },
            "required": ["condition", "temperature", "optional_source_metric"],
        },
        field_sources={
            "condition": "Condition",
            "temperature": "Temperature",
            "optional_source_metric": "Supplemental Metric",
        },
        field_types={
            "condition": "text",
            "temperature": "number",
            "optional_source_metric": "number",
        },
    )
    response = SimpleNamespace(
        content=json.dumps({
            "found": True,
            "rows": [{
                "condition": "Cloudy",
                "temperature": 28,
                "optional_source_metric": None,
            }],
            "end_visible": True,
            "scope_satisfied": True,
            "evidence": "one visible row",
        }),
        usage_metadata={},
        response_metadata={},
    )
    observed_messages = []
    vision = SimpleNamespace()
    vision.bind = lambda **_kwargs: SimpleNamespace(
        invoke=lambda messages: observed_messages.extend(messages) or response
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision = vision
    materializer.task_goal = "Return tomorrow's forecast"

    extracted = PerceptionMaterializer._vision_extract(
        materializer,
        requirement,
        b"png",
        acquisition_filters={},
        page_identity={"url": "https://example.test/records", "title": "Records"},
    )

    assert len(extracted["rows"]) == 1
    assert extracted["rows"][0]["optional_source_metric"] is None
    human_text = observed_messages[-1].content[0]["text"]
    assert "Original task temporal context: Return tomorrow's forecast" in human_text
    assert "Task reference time (frozen platform clock):" in human_text
    assert 'Frozen relative calendar dates by day offset: {"-2":' in human_text
    assert 'Current page identity: {"url": "https://example.test/records"' in human_text


def test_optional_visual_null_is_omitted_instead_of_rejecting_row(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="records",
        description="Visible records with an optional source field",
        row_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "optional_note": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        field_sources={"name": "Name", "optional_note": "Note"},
        field_types={"name": "text", "optional_note": "text"},
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"name": "visible", "optional_note": None}],
        "end_visible": True,
        "scope_satisfied": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.collections[0].row_count == 1
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [
        {"name": "visible"}
    ]


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


def test_unmapped_structured_content_allows_visual_field_completion(
    tmp_path: Path,
) -> None:
    properties = {
        "name": {"type": "string"},
        "modified": {"type": "string"},
        "kind": {"type": "string"},
    }
    requirement = DataRequirement(
        id="files",
        description="Visible file rows",
        row_schema={"type": "object", "properties": properties,
                    "required": list(properties)},
        field_sources={"name": "Name", "modified": "Modified", "kind": "Kind"},
    )
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"name": "a.zip", "modified": "Jul 11", "kind": "ZIP archive"}],
        "end_visible": True,
        "scope_satisfied": True,
    }
    table = {
        "headers": ["Name", "Details"],
        "rows": [{"Name": "a.zip", "Details": ["Jul 11", "ZIP archive"]}],
        "unmapped_visible_content": True,
        "in_viewport": True,
        "partial": False,
        "traversal": {"type": "static"},
    }

    frame = _observe_requirement(materializer, requirement, 1, tables=[table])

    assert frame.chunks[0].provider == "vision"
    assert "detail_resolution" not in frame.requirement_scopes["files"]
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [{
        "name": "a.zip", "modified": "Jul 11", "kind": "ZIP archive",
    }]


def test_visual_bottom_does_not_override_enabled_surface_pagination(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="records",
        description="Order List",
        target_label="Order List",
        row_schema={
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
        field_sources={"record_id": "Record ID"},
        field_types={"record_id": "text"},
    )
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"record_id": "1"}],
        "end_visible": True,
        "scope_satisfied": True,
    }
    table = {
        "caption": "Order List",
        "headers": ["ID"],
        "rows": [{"ID": "1"}],
        "partial": False,
        "traversal": {
            "type": "paged",
            "page_index": 1,
            "page_count": 4,
            "has_next_page": True,
            "has_prev_page": False,
        },
    }
    frame, _ = materializer.observe(
        bundle=FakeBundle([table]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    coverage = frame.collections[0].coverage
    assert frame.chunks[0].provider == "vision"
    assert coverage["status"] == "incomplete"
    assert coverage["at_end"] is False
    assert coverage["pages_seen"] == [1]
    assert coverage["movement"]["has_next_page"] is True


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
        filters={"product": "Candidate Product"},
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
        "start_visible": True,
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
    assert frame.collections[0].coverage["status"] == "incomplete"
    assert materializer.data_store.mark_scroll_end(
        frame.collections[0].ref
    ).coverage["status"] == "complete"
    clipped = _materializer(tmp_path, "vision-only")
    clipped._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True, "rows": [{"term": "visible", "uses": 2}],
        "clipped_top_record_visible": True, "start_visible": True,
        "end_visible": True,
    }
    frame = _observe_requirement(clipped, _requirement(), 2)
    completed = clipped.data_store.mark_scroll_end(frame.collections[0].ref)
    assert (frame.collections[0].coverage["start_seen"],
            completed.coverage["status"]) == (False, "incomplete")


def test_visual_singleton_completes_without_collection_boundary(tmp_path: Path) -> None:
    requirement = _requirement().model_copy(update={"cardinality": "one"})
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"term": "authoritative summary", "uses": 27}],
        "start_visible": True,
        "end_visible": False,
    }

    frame = _observe_requirement(materializer, requirement, 1)

    assert frame.collections[0].row_count == 1
    assert frame.collections[0].coverage["status"] == "complete"
    assert frame.collections[0].coverage["cardinality"] == "one"


def test_single_identity_candidates_assemble_linked_details(tmp_path: Path) -> None:
    properties = {"name": {"type": "string"}, "content": {
        "type": "array", "items": {"type": "string"},
    }}
    requirement = DataRequirement(
        id="documents", description="Documents and their content",
        row_schema={"type": "object", "properties": properties,
                    "required": list(properties)},
        field_sources={"name": "Name", "content": "Content"},
        field_types={"name": "text", "content": "text_list"},
    )
    materializer = _materializer(tmp_path, "enhanced")
    candidate_table = {
        "headers": ["Name"],
        "rows": [{"Name": "a.txt"}, {"Name": "b.txt"}],
        "partial": True,
        "traversal": {"type": "scroll"},
    }
    first = _observe_requirement(
        materializer, requirement, 1, tables=[candidate_table],
    )
    assert first.missing_requirements == ["documents"]

    def detail(name: str, content: str) -> dict:
        return {
            "source": "webview-document",
            "headers": ["Name", "Content"],
            "rows": [{"Name": name, "Content": content}],
            "total_records": 1,
            "partial": False,
            "traversal": {"type": "static", "has_next_page": False},
        }

    second = _observe_requirement(
        materializer, requirement, 2, tables=[detail("a.txt", "one\ntwo\n")],
    )
    assert second.missing_requirements == ["documents"]
    assert second.requirement_scopes["documents"]["detail_resolution"]["status"] == "active"

    completed = _observe_requirement(
        materializer, requirement, 3, tables=[detail("b.txt", "three\n")],
    )
    collection = completed.collections[0]
    assert collection.coverage["status"] == "complete"
    assert materializer.data_store.collection_rows(collection.ref) == [
        {"name": "a.txt", "content": ["one", "two"]},
        {"name": "b.txt", "content": ["three"]},
    ]


def test_bound_acquisition_defers_and_nested_surface_owns_candidates(
    tmp_path: Path,
) -> None:
    properties = {
        "name": {"type": "string"},
        "content": {"type": "array", "items": {"type": "string"}},
    }
    requirement = DataRequirement(
        id="documents", description="Selected documents and their content",
        row_schema={"type": "object", "properties": properties,
                    "required": list(properties)},
        field_sources={"name": "Name", "content": "Content"},
        field_types={"name": "text", "content": "text_list"},
    )
    materializer = _materializer(tmp_path, "enhanced")

    def candidates(location: str, *names: str) -> dict:
        return {
            "path": "native-list", "location": location, "headers": ["Name"],
            "rows": [{"Name": name} for name in names],
            "partial": True, "traversal": {"type": "scroll"},
        }

    deferred = _observe_requirement(
        materializer, requirement, 1,
        tables=[candidates("Downloads", "unrelated.zip")],
        allow_linked_details=False,
    )
    assert "detail_resolution" not in deferred.requirement_scopes["documents"]

    target = _observe_requirement(
        materializer, requirement, 2,
        tables=[candidates("Downloads", "target.zip")],
    )
    assert target.requirement_scopes["documents"]["detail_resolution"][
        "next_unresolved_candidate"
    ]["fields"] == {"name": "target.zip"}

    overlay = _observe_requirement(
        materializer, requirement, 3,
        tables=[candidates("Open with", "OpenDocument Reader")],
    )
    assert overlay.requirement_scopes["documents"]["detail_resolution"][
        "next_unresolved_candidate"
    ]["fields"] == {"name": "target.zip"}

    archive = _observe_requirement(
        materializer, requirement, 4,
        tables=[candidates("Downloads > target.zip", "a.txt", "b.txt")],
    )
    assert archive.requirement_scopes["documents"]["detail_resolution"][
        "candidate_records"
    ] == 2

    parent = _observe_requirement(
        materializer, requirement, 5,
        tables=[candidates("Downloads", "unrelated.txt")],
    )
    assert parent.requirement_scopes["documents"]["detail_resolution"][
        "candidate_records"
    ] == 2
    assert parent.requirement_scopes["documents"]["detail_resolution"][
        "next_unresolved_candidate"
    ]["fields"] == {"name": "a.txt"}


def test_explicit_visual_empty_state_materializes_complete_empty_collection(
    tmp_path: Path,
) -> None:
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": False,
        "rows": [],
        "empty_state_visible": True,
        "empty_state_evidence": "No matching records",
        "end_visible": True,
        "scope_satisfied": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    collection = frame.collections[0]
    assert collection.row_count == 0
    assert collection.coverage["status"] == "complete"
    assert collection.coverage["coverage_evidence"] == "explicit_visual_empty_state"
    assert collection.coverage["empty_state_evidence"] == "No matching records"
    assert frame.missing_requirements == []


def test_confirmed_visual_filter_supplies_exact_scope_fields(tmp_path: Path) -> None:
    requirement = _requirement().model_copy(update={"filters": {"term": "fixed"}})
    assert set(_visible_row_schema(requirement, requirement.filters)["properties"]) == {"uses"}

    broader = _visible_row_schema(requirement, {"term": "fix"})
    assert set(broader["properties"]) == {"term", "uses"}
    materializer = _materializer(tmp_path, "vision-only")
    extracts = iter([
        {"filter_state_visible": True, "filter_commit_pending": True},
        {"found": True, "rows": [{"uses": 20}], "start_visible": True,
         "end_visible": True},
    ])
    materializer._vision_extract = lambda *_args, **_kwargs: next(extracts)  # type: ignore[method-assign]

    _observe_requirement(
        materializer, requirement, 1, acquisition_filters={"term": "fixed"},
    )
    frame = _observe_requirement(
        materializer, requirement, 2, acquisition_filters={"term": "fixed"},
    )

    assert (frame.requirement_scopes["terms"]["status"],
            materializer.data_store.collection_rows(frame.collections[0].ref)) == (
                "met", [{"term": "fixed", "uses": 20}],
            )


def test_visual_empty_state_requires_explicit_evidence(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": False,
        "rows": [],
        "empty_state_visible": True,
        "empty_state_evidence": "",
        "end_visible": True,
        "scope_satisfied": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert frame.collections == []
    assert frame.missing_requirements == ["terms"]


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
