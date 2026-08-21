from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gui_agent.adapters.browser.table_reader import normalize_table_snapshots
from gui_agent.core.tool_agent.filter_state import (
    AppliedFilterState,
    compile_filter_predicates,
)
from gui_agent.core.tool_agent.contracts import DataRequirement
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.perception import (
    PerceptionMaterializer,
    _match_table,
    _source_keys,
    _structured_rows,
    _visual_collection_key,
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


def test_product_card_fields_preserve_percentage_rating_semantics() -> None:
    requirement = DataRequirement(
        id="products",
        description="Ranked products",
        row_schema={
            "type": "object",
            "properties": {
                "product_name": {"type": "string"},
                "rating": {"type": "number"},
                "review_count": {"type": "number"},
                "price": {"type": "number"},
            },
            "required": ["product_name", "rating", "review_count", "price"],
        },
        field_sources={
            "product_name": "Product Name",
            "rating": "Rating",
            "review_count": "Reviews",
            "price": "Price",
        },
    )

    keys = _source_keys(requirement, {
        "headers": [
            "productName", "price", "reviewCount", "ratingPercentage", "ratingValue",
        ],
        "rows": [],
    })

    assert keys == {
        "product_name": "productname",
        "rating": "ratingpercentage",
        "review_count": "reviewcount",
        "price": "price",
    }


def test_structured_rows_reject_blank_required_number_without_aborting() -> None:
    requirement = DataRequirement(
        id="products",
        description="Rated products",
        row_schema={
            "type": "object",
            "properties": {
                "product_name": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["product_name", "rating"],
        },
        field_sources={"product_name": "Product Name", "rating": "Rating"},
        field_types={"product_name": "text", "rating": "number"},
    )

    rows = _structured_rows(requirement, {
        "headers": ["productName", "ratingPercentage"],
        "rows": [
            {"productName": "Rated", "ratingPercentage": 100},
            {"productName": "Unrated", "ratingPercentage": ""},
        ],
    })

    assert rows == [{"product_name": "Rated", "rating": 100.0}]


def test_known_target_caption_rejects_field_compatible_sibling_surface() -> None:
    requirement = DataRequirement(
        id="invoices",
        description="Invoice records",
        target_label="Invoices",
        row_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        field_sources={"name": "Name"},
    )
    sibling = {"caption": "Dashboard", "headers": ["Name"], "rows": []}
    target = {"caption": "Invoices", "headers": ["Name"], "rows": []}

    assert _match_table(requirement, [sibling]) is None
    assert _match_table(requirement, [sibling, target]) is target


def test_visual_collection_key_isolates_routes_but_ignores_page_number() -> None:
    requirement = _requirement()
    scope = {"requested_filters": {"Status": "open"}}
    table = {
        "caption": "Records",
        "traversal": {"type": "paged"},
    }

    first = _visual_collection_key(
        requirement, scope, table, "https://example.test/orders?p=1",
    )
    second = _visual_collection_key(
        requirement, scope, table, "https://example.test/orders?p=2",
    )
    residue = _visual_collection_key(
        requirement, scope, table, "https://example.test/dashboard?p=1",
    )

    assert first == second
    assert first != residue


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
        observation: dict | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.control_state = control_state
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state
        self.collection_regions = collection_regions
        self.observation = observation or {}

    def observe(self):
        values = dict(
            png_bytes=b"png",
            loading=None,
            tables=self.tables,
            form_controls=self.controls,
            form_control_state=self.control_state,
            applied_filters=self.applied_filters,
            applied_filter_state=self.applied_filter_state,
            collection_regions=self.collection_regions,
            url="https://example.test",
            title="Example",
        )
        return SimpleNamespace(**(values | self.observation))


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
        observation: dict | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.control_state = control_state
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state
        self.collection_regions = collection_regions
        self.observation = observation
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
            observation=self.observation,
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
    materializer._source_field_capabilities = {}
    materializer._vision_extract = lambda _requirement, _png, **_kwargs: {  # type: ignore[method-assign]
        "found": False,
        "rows": [],
        "end_visible": False,
    }
    return materializer


def test_materialized_frame_preserves_page_viewport_boundaries(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path, "enhanced")
    viewport = {
        "type": "scroll",
        "at_scroll_start": True,
        "at_scroll_end": False,
        "can_scroll_more": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([], observation={"viewport": viewport}),
        platform=FakePlatform(),
        requirements=[],
        frame_no=1,
    )

    assert frame.page_viewport == viewport


def test_sparse_window_retains_same_source_field_capability(tmp_path: Path) -> None:
    requirement = DataRequirement(
        id="products",
        description="Products with ratings",
        target_label="Products",
        row_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "rating": {"type": "number"},
                "price": {"type": "number"},
            },
            "required": ["name", "rating", "price"],
            "additionalProperties": False,
        },
        field_sources={
            "name": "Product Name", "rating": "Rating", "price": "Price",
        },
    )
    materializer = _materializer(tmp_path, "enhanced")
    complete = {
        "caption": "Products",
        "headers": ["productName", "ratingValue", "price"],
        "rows": [{"productName": "Rated", "ratingValue": 5, "price": 20}],
        "partial": False,
        "traversal": {"type": "paged", "page_index": 1, "page_count": 2},
    }
    sparse = {
        "caption": "Products",
        "headers": ["productName", "price"],
        "rows": [{"productName": "Unrated", "price": 10}],
        "partial": False,
        "traversal": {"type": "paged", "page_index": 2, "page_count": 2},
    }

    materializer.observe(
        bundle=FakeBundle(
            [complete], observation={"url": "https://shop.test/products?p=1"},
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        state_scope="worker-a",
        frame_no=1,
    )
    frame, _ = materializer.observe(
        bundle=FakeBundle(
            [sparse], observation={"url": "https://shop.test/products?p=2"},
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        state_scope="worker-a",
        frame_no=2,
    )

    assert "detail_resolution" not in frame.requirement_scopes[requirement.id]
    assert frame.requirement_scopes[requirement.id]["query_outcome"] == (
        "no_matching_rows_on_complete_page"
    )


def test_source_field_capability_does_not_cross_resource_path(tmp_path: Path) -> None:
    requirement = DataRequirement(
        id="products",
        description="Products with ratings",
        target_label="Products",
        row_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "rating": {"type": "number"},
            },
            "required": ["name", "rating"],
            "additionalProperties": False,
        },
        field_sources={"name": "Name", "rating": "Rating"},
    )
    materializer = _materializer(tmp_path, "enhanced")
    materializer.observe(
        bundle=FakeBundle([{
            "caption": "Products", "headers": ["Name", "Rating"],
            "rows": [{"Name": "A", "Rating": 5}], "partial": False,
        }], observation={"url": "https://shop.test/products"}),
        platform=FakePlatform(), requirements=[requirement],
        state_scope="worker-a", frame_no=1,
    )
    frame, _ = materializer.observe(
        bundle=FakeBundle([{
            "caption": "Products", "headers": ["Name"],
            "rows": [{"Name": "B"}], "partial": False,
        }], observation={"url": "https://shop.test/archive"}),
        platform=FakePlatform(), requirements=[requirement],
        state_scope="worker-a", frame_no=2,
    )

    assert frame.requirement_scopes[requirement.id]["detail_resolution"]["status"] == "active"


def test_source_field_capability_does_not_cross_nonpagination_query(tmp_path: Path) -> None:
    requirement = DataRequirement(
        id="products",
        description="Products with ratings",
        target_label="Products",
        row_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "rating": {"type": "number"},
            },
            "required": ["name", "rating"],
            "additionalProperties": False,
        },
        field_sources={"name": "Name", "rating": "Rating"},
    )
    materializer = _materializer(tmp_path, "enhanced")
    full = {
        "caption": "Products", "headers": ["Name", "Rating"],
        "rows": [{"Name": "A", "Rating": 5}], "partial": False,
        "traversal": {"type": "paged", "page_index": 1, "page_count": 2},
    }
    sparse = {
        "caption": "Products", "headers": ["Name"],
        "rows": [{"Name": "B"}], "partial": False,
        "traversal": {"type": "paged", "page_index": 1, "page_count": 2},
    }
    materializer.observe(
        bundle=FakeBundle(
            [full], observation={"url": "https://shop.test/products?q=boots&p=1"},
        ),
        platform=FakePlatform(), requirements=[requirement],
        state_scope="worker-a", frame_no=1,
    )
    frame, _ = materializer.observe(
        bundle=FakeBundle(
            [sparse], observation={"url": "https://shop.test/products?q=shoes&p=1"},
        ),
        platform=FakePlatform(), requirements=[requirement],
        state_scope="worker-a", frame_no=2,
    )

    assert frame.requirement_scopes[requirement.id]["detail_resolution"]["status"] == "active"


def _observe_requirement(
    materializer: PerceptionMaterializer,
    requirement: DataRequirement,
    frame_no: int,
    *,
    tables: list[dict] | None = None,
    allow_linked_details: bool = True,
):
    return materializer.observe(
        bundle=FakeBundle(tables or []),
        platform=FakePlatform(),
        requirements=[requirement],
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


def _blank_png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1280, 800), "white").save(output, format="PNG")
    return output.getvalue()


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


@pytest.mark.parametrize(
    ("platform_loading", "expected_readiness"),
    [(True, "loading"), (False, "blank")],
)
def test_unready_frame_skips_visual_collection_extraction(
    tmp_path: Path,
    platform_loading: bool,
    expected_readiness: str,
) -> None:
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("unready frame must not invoke visual extraction")
    )

    frame, _ = materializer.observe(
        bundle=FakeBundle([], observation={
            "loading": platform_loading, "png_bytes": _blank_png(),
        }),
        platform=FakePlatform(),
        requirements=[_requirement()],
        frame_no=1,
    )

    assert frame.readiness == expected_readiness
    assert frame.chunks == []
    assert frame.collections == []
    assert frame.missing_requirements == ["terms"]


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


def test_text_query_can_broaden_without_changing_required_predicates(
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
            controls=[{
                "kind": "text_input",
                "label": "Product",
                "value": "Erica",
                "is_filter": True,
            }],
            applied_filters={"Product": "Erica"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({"Product": "Erica"}),
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.requirement_scopes["reviews"]["requested_filters"] == {
        "Product": "Erica Sports Bra"
    }
    assert frame.requirement_scopes["reviews"]["query_state"]["filters"] == {
        "Product": "Erica"
    }
    assert materializer.data_store.collection_rows(frame.collections[0].ref) == [
        {"product": "Erica Sports Bra", "title": "Target review"},
    ]
    assert frame.collections[0].coverage["status"] == "complete"

    stale, _ = materializer.observe(
        bundle=FakeBundle(
            tables,
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

    assert stale.requirement_scopes["reviews"]["status"] == "unmet"
    assert stale.requirement_scopes["reviews"]["evidence"] == (
        "conflicting_non_query_filters"
    )
    # Under pure ReAct, prior accum may be retained across filter views (collection
    # belongs to the worker). The key signal is the unmet scope + conflicting evidence.


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
        frame_no=1,
    )

    scope = frame.requirement_scopes[requirement.id]
    assert scope["status"] == "unknown"
    assert scope["filter_rejected_rows"] == 1
    assert frame.collections == []
    assert frame.missing_requirements == [requirement.id]


def test_semantic_contains_filter_defers_to_perception(
    tmp_path: Path,
) -> None:
    """`<field>_contains` marks prose: a paraphrase row accumulates instead of being dropped."""
    requirement = DataRequirement(
        id="small_ear_cup_reviews",
        description="Reviews mentioning small ear cups with reviewer name",
        row_schema={
            "type": "object",
            "properties": {
                "reviewer_name": {"type": "string"},
                "review_text": {"type": "string"},
            },
            "required": ["reviewer_name", "review_text"],
        },
        field_sources={"reviewer_name": "Reviewer Name", "review_text": "Review Text"},
        field_types={"reviewer_name": "text", "review_text": "text"},
        filters={"review_text_contains": "ear cups being small"},
        coverage="complete",
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{
            "reviewer_name": "Catso",
            "review_text": "they really are for people with very small ears",
        }],
        "end_visible": False,
        "start_visible": True,
        "scope_satisfied": None,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    scope = frame.requirement_scopes[requirement.id]
    # Deterministic scope is not hard-met, but the row is NOT rejected either.
    assert scope.get("filter_rejected_rows") != 1
    assert frame.collections, "paraphrase row must accumulate into a collection"
    collection = frame.collections[0]
    assert collection.row_count == 1
    assert frame.missing_requirements == []


def test_eq_phrase_predicate_defers_to_perception(
    tmp_path: Path,
) -> None:
    """A multi-token exact predicate on a text field is prose, not an identity label."""
    requirement = DataRequirement(
        id="reviews",
        description="Reviews mentioning small ear cups",
        row_schema={"reviewer_name": "string", "review_text": "string"},
        field_sources={"reviewer_name": "Reviewer Name", "review_text": "Review Text"},
        field_types={"reviewer_name": "text", "review_text": "text"},
        filters={"review_text": "ear cups being small"},
        coverage="complete",
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{
            "reviewer_name": "Catso",
            "review_text": "they really are for people with very small ears",
        }],
        "end_visible": False,
        "start_visible": True,
        "scope_satisfied": None,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.collections
    assert frame.collections[0].row_count == 1
    assert frame.missing_requirements == []
    assert _rows_satisfy_filters(requirement, [{
        "reviewer_name": "Catso",
        "review_text": "they really are for people with very small ears",
    }]) is None


def test_semantic_scope_satisfied_false_is_not_a_hard_block(
    tmp_path: Path,
) -> None:
    """A semantic predicate has no UI filter to apply; perception's
    scope_satisfied=False is informational and must not mark the scope unmet
    (which would drive the Worker to seek a nonexistent filter and report
    blocked, triggering a strategy replacement churn)."""
    requirement = DataRequirement(
        id="reviews",
        description="Reviews mentioning small ear cups",
        row_schema={"reviewer_name": "string", "review_text": "string"},
        field_sources={"reviewer_name": "Reviewer Name", "review_text": "Review Text"},
        field_types={"reviewer_name": "text", "review_text": "text"},
        filters={"review_text_contains": "ear cups being small"},
        coverage="complete",
    )
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{
            "reviewer_name": "Somebody",
            "review_text": "these are for people with very small ears",
        }],
        "end_visible": False,
        "start_visible": True,
        "scope_satisfied": False,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    scope = frame.requirement_scopes[requirement.id]
    assert scope["status"] != "unmet"
    assert frame.collections, "rows still accumulate despite scope_satisfied=False"


def test_screen_reader_transcribes_all_then_judge_filters_semantic_rows(
    tmp_path: Path,
) -> None:
    """The dedicated screen reader transcribes every visible record; for a semantic
    predicate a text judge keeps only the matching subset (live gap: the fused
    transcribe-and-filter perception dropped the paraphrase review)."""
    requirement = DataRequirement(
        id="reviews",
        description="Reviews mentioning small ear cups",
        row_schema={"reviewer_name": "string", "review_text": "string"},
        field_sources={"reviewer_name": "Reviewer Name", "review_text": "Review Text"},
        field_types={"reviewer_name": "text", "review_text": "text"},
        filters={"review_text_contains": "ear cups being small"},
        coverage="complete",
    )
    all_rows = [
        {"reviewer_name": "Catso", "review_text": "for people with very small ears"},
        {"reviewer_name": "Jenna", "review_text": "battery lasts forever"},
        {"reviewer_name": "Anglebert", "review_text": "got about half my ear into it"},
        {"reviewer_name": "JGonzo", "review_text": "the gym music is loud"},
    ]
    read_response = SimpleNamespace(
        content=json.dumps({
            "found": True,
            "rows": all_rows,
            "end_visible": False,
            "scope_satisfied": None,
        }),
        usage_metadata={},
        response_metadata={},
    )
    judge_response = SimpleNamespace(
        content=json.dumps({"matched_indices": [0, 2]}),
        usage_metadata={},
        response_metadata={},
    )
    calls = []

    def fake_bind(**kwargs):
        def invoke(messages):
            calls.append(messages)
            # screen reader message has an image part; the judge is text-only
            if any(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in messages[-1].content
            ):
                return read_response
            return judge_response
        return SimpleNamespace(invoke=invoke)

    materializer = _materializer(tmp_path, "vision-only")
    materializer._screen_reader = SimpleNamespace(bind=fake_bind)  # type: ignore[assignment]

    extracted = PerceptionMaterializer._vision_extract(
        materializer,
        requirement,
        b"png",
        query_filters={},
        page_identity={"url": "https://example.test", "title": "Reviews"},
    )

    assert len(calls) == 2, "screen read + semantic judge both called"
    names = [row["reviewer_name"] for row in extracted["rows"]]
    assert names == ["Catso", "Anglebert"], (
        "judge keeps the paraphrase match (Anglebert) and drops non-matches"
    )
    judge_prompt = calls[1][-1].content
    assert "Requirement context:" in judge_prompt
    assert "apply any explicit taxonomy equivalence" in judge_prompt
    assert "record's primary subject" in judge_prompt
    assert "explicit bundle inclusion is a match" in judge_prompt
    assert "compatibility or accessory mentions" in judge_prompt
    assert "primary function to perform the requested function" in judge_prompt
    assert "unless the requested class explicitly includes those objects" in judge_prompt
    assert extracted["found"] is True


def test_numeric_bound_predicate_is_deterministic_not_semantic() -> None:
    requirement = DataRequirement(
        id="reviews",
        description="Reviews with a low star rating",
        row_schema={"reviewer_name": "string", "star_rating": "number"},
        field_sources={"reviewer_name": "Reviewer Name", "star_rating": "Star Rating"},
        field_types={"reviewer_name": "text", "star_rating": "number"},
        filters={"star_rating": {"max": 3}},
        coverage="complete",
    )
    # A numeric bound is decided deterministically, never deferred to perception.
    assert _rows_satisfy_filters(requirement, [
        {"reviewer_name": "a", "star_rating": 1},
    ]) is True
    assert _rows_satisfy_filters(requirement, [
        {"reviewer_name": "a", "star_rating": 4},
    ]) is False


def test_datetime_bound_compares_instants_across_offsets() -> None:
    requirement = DataRequirement(
        id="records",
        description="Records in a date interval",
        row_schema={"recorded_at": "string"},
        field_sources={"recorded_at": "Recorded At"},
        field_types={"recorded_at": "datetime"},
        filters={"recorded_at": {
            "min": "2022-09-01T00:00:00+08:00",
            "max": "2022-09-30T23:59:59+08:00",
        }},
    )

    assert _rows_satisfy_filters(requirement, [
        {"recorded_at": "2022-09-01T00:00:00+00:00"},
    ]) is True
    assert _rows_satisfy_filters(requirement, [
        {"recorded_at": "2022-08-31T15:59:59+00:00"},
    ]) is False


def test_structured_list_snapshot_deterministically_reads_reviews(
    tmp_path: Path,
) -> None:
    """Replay: the real Epson review DOM (captured as a list snapshot) reads
    deterministically — correct name-text pairing and star ratings, so the gold
    reviewers surface without any vision. The semantic judge is stubbed to the
    literal phrase; its paraphrase role is covered separately."""
    raw = json.loads(
        (Path(__file__).parent / "fixtures/tool_agent/task25_epson_list_snapshot.json")
        .read_text(encoding="utf-8")
    )
    snaps = normalize_table_snapshots(raw)
    assert snaps and snaps[0]["source"] == "list"
    assert len(snaps[0]["rows"]) == 10

    requirement = DataRequirement(
        id="print_quality_low_rating_reviews",
        description="Reviews mentioning print quality with rating 3 or less",
        row_schema={
            "type": "object",
            "properties": {
                "reviewer_name": {"type": "string"},
                "review_title": {"type": "string"},
                "star_rating": {"type": "number"},
                "review_text": {"type": "string"},
            },
            "required": ["reviewer_name", "star_rating", "review_text"],
        },
        field_sources={
            "reviewer_name": "reviewer name",
            "review_title": "review title",
            "star_rating": "star rating",
            "review_text": "review text",
        },
        field_types={
            "reviewer_name": "text", "review_title": "text",
            "star_rating": "number", "review_text": "text",
        },
        filters={"star_rating": {"max": 3}, "review_text_contains": "print quality"},
        coverage="complete",
    )
    materializer = _materializer(tmp_path, "enhanced")
    materializer._semantic_judge = (  # type: ignore[method-assign]
        lambda _req, rows: [
            r for r in rows if "print quality" in (r.get("review_text") or "").lower()
        ]
    )

    frame, _ = materializer.observe(
        bundle=FakeBundle(snaps),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    rows = materializer.data_store.collection_rows(frame.collections[0].ref)
    names = sorted(row["reviewer_name"] for row in rows)
    # Gold (1-star reviewers who mention print quality) — Roxanne and Nelson; the
    # 4-star reviewers (Goldfish/Imajin8) and non-print-quality 1-stars are excluded.
    assert names == ["Nelson", "Roxanne Brandon Coffey"]
    assert all(row.get("review_title") for row in rows)


def test_unrelated_surface_total_does_not_contaminate_visual_collection(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="reviews",
        description="Product review titles",
        row_schema={"review_title": "string"},
        field_sources={"review_title": "review title"},
        field_types={"review_title": "text"},
        coverage="complete",
    )
    unrelated = {
        "caption": "Product Description",
        "headers": ["Feature", "Value"],
        "rows": [{"Feature": "Capacity", "Value": "16 GB"}],
        "total_records": 2000,
        "partial": True,
        "in_viewport": True,
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"review_title": "Came Defective"}],
        "start_visible": True,
        "end_visible": False,
        "scope_satisfied": None,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle([unrelated]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.collections[0].coverage["known_total"] is None


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
        query_filters={},
        page_identity={"url": "https://example.test/records", "title": "Records"},
    )

    assert len(extracted["rows"]) == 1
    assert extracted["rows"][0]["optional_source_metric"] is None
    human_text = observed_messages[-1].content[0]["text"]
    # The dedicated screen reader is pure transcription: it carries the page
    # identity and row schema but no temporal/predicate bias (matching is a
    # separate text-judge step).
    assert "Current page identity: {\"url\": \"https://example.test/records\"" in human_text
    assert "transcribe EVERY visible record, matching or not" in human_text
    assert "Original task temporal context" not in human_text


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
        controls=[{
            "kind": "text_input",
            "label": "Product",
            "value": "Candidate",
            "is_filter": True,
        }],
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


def test_structured_pagination_accumulates_across_page_query_urls(tmp_path: Path) -> None:
    materializer = _materializer(tmp_path, "enhanced")

    def observe(page: int, terms: list[str]):
        table = {
            "caption": "Top Terms",
            "headers": ["Search Term", "Uses"],
            "rows": [{"Search Term": term, "Uses": "1"} for term in terms],
            "partial": page == 1,
            "traversal": {
                "type": "paged",
                "page_index": page,
                "page_count": 2,
                "has_next_page": page == 1,
                "has_prev_page": page == 2,
            },
        }
        frame, _ = materializer.observe(
            bundle=FakeBundle(
                [table],
                observation={"url": f"https://example.test/terms?p={page}"},
            ),
            platform=FakePlatform(),
            requirements=[_requirement()],
            frame_no=page,
        )
        return frame

    first = observe(1, ["alpha", "beta"])
    completed = observe(2, ["gamma"])

    assert first.collections[0].row_count == 2
    assert completed.collections[0].row_count == 3
    assert completed.collections[0].coverage["pages_seen"] == [1, 2]
    assert completed.collections[0].coverage["status"] == "complete"
    assert materializer.data_store.collection_rows(completed.collections[0].ref) == [
        {"term": "alpha", "uses": 1},
        {"term": "beta", "uses": 1},
        {"term": "gamma", "uses": 1},
    ]


@pytest.mark.parametrize("rows,total,filters", [
    ([], 0, {}),
    ([{"Search Term": "visible", "Uses": "1"}], 1, {"uses": {"max": 0}}),
])
def test_enhanced_materializes_a_complete_empty_structured_collection(
    tmp_path: Path,
    rows: list[dict[str, str]],
    total: int,
    filters: dict,
) -> None:
    tables = [{
        "caption": "Top Terms",
        "headers": ["Search Term", "Uses"],
        "rows": rows,
        "total_records": total,
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
        requirements=[_requirement().model_copy(update={"filters": filters})],
        frame_no=1,
    )

    assert frame.missing_requirements == []
    assert frame.chunks[0].provider == "structured"
    assert frame.collections[0].row_count == 0
    assert frame.collections[0].coverage["status"] == "complete"


def test_filtered_empty_partial_structured_surface_remains_missing(tmp_path: Path) -> None:
    table = {
        "caption": "Top Terms",
        "rows": [{"Search Term": "visible", "Uses": "1"}],
        "total_records": 10,
        "partial": True,
        "traversal": {"type": "paged", "page_index": 1, "page_count": 2},
    }
    materializer = _materializer(tmp_path, "enhanced")
    requirement = _requirement().model_copy(update={"filters": {"uses": {"max": 0}}})

    frame, _ = materializer.observe(
        bundle=FakeBundle([table]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    assert frame.collections == []
    assert frame.missing_requirements == [requirement.id]


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


def test_assembled_details_replace_a_stale_empty_collection(tmp_path: Path) -> None:
    """A complete 0-row query must not block a later assembled walk."""

    requirement = DataRequirement(
        id="review_details",
        description="Review details",
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
    )
    empty = {
        "caption": "All Reviews",
        "headers": ["Product", "Title", "Action"],
        "rows": [],
        "total_records": 0,
        "partial": False,
        "traversal": {"type": "static", "has_next_page": False},
    }
    listed = {
        "caption": "All Reviews",
        "headers": ["Product", "Title", "Action"],
        "rows": [
            {"Product": "Widget A", "Title": "Ann", "Action": "Edit"},
            {"Product": "Widget B", "Title": "Bob", "Action": "Edit"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    vision_calls = []

    def vision_only_for_empty_query(*_a, **_k):
        vision_calls.append(1)
        if len(vision_calls) > 1:
            raise AssertionError(
                "structured list/detail evidence must not invoke visual extraction"
            )
        return {
            "found": False,
            "rows": [],
            "empty_state_visible": True,
            "empty_state_evidence": "No records found",
            "end_visible": True,
            "filter_state_visible": True,
            "scope_satisfied": True,
        }

    materializer._vision_extract = vision_only_for_empty_query  # type: ignore[method-assign]

    def observe(frame_no: int, *, table=None, controls=None, applied=None, query=None):
        scoped = requirement.model_copy(update={"filters": query or {}})
        return materializer.observe(
            bundle=FakeBundle(
                [table] if table else [],
                controls=controls or [],
                applied_filters=applied or {},
                applied_filter_state=(
                    AppliedFilterState(
                        predicates=compile_filter_predicates(applied),
                        coverage="complete",
                        source="filter_indicator",
                    )
                    if applied
                    else None
                ),
            ),
            platform=FakePlatform(),
            requirements=[scoped],
            frame_no=frame_no,
        )[0]

    empty_frame = observe(
        1, table=empty, applied={"Product": "Widget C"}, query={"product": "Widget C"},
    )
    assert empty_frame.collections[0].row_count == 0
    assert empty_frame.collections[0].coverage["status"] == "complete"

    observe(2, table=listed)
    observe(
        3,
        controls=[
            {"label": "Title", "kind": "text_input", "value": "Ann"},
            {"label": "Detailed Rating", "kind": "number", "value": 3},
        ],
    )
    last = observe(
        4,
        controls=[
            {"label": "Title", "kind": "text_input", "value": "Bob"},
            {"label": "Detailed Rating", "kind": "number", "value": 2},
        ],
    )
    assert last.collections[0].row_count == 2
    assert last.collections[0].coverage["status"] == "complete"


def test_exact_text_query_empty_is_not_an_authoritative_empty_result(
    tmp_path: Path,
) -> None:
    requirement = DataRequirement(
        id="reviews",
        description="Reviews for one exact product",
        row_schema={"product": "string", "title": "string"},
        field_sources={"product": "Product", "title": "Title"},
        filters={"product": "Erica Sports Bra"},
    )
    empty_table = {
        "caption": "Reviews",
        "headers": ["Product", "Title"],
        "rows": [],
        "total_records": 0,
        "partial": False,
        "traversal": {"type": "static", "has_next_page": False},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": False,
        "rows": [],
        "empty_state_visible": True,
        "empty_state_evidence": "No records found",
        "end_visible": True,
        "filter_state_visible": True,
        "scope_satisfied": True,
    }

    frame, _ = materializer.observe(
        bundle=FakeBundle(
            [empty_table],
            controls=[{
                "kind": "text_input",
                "label": "Product",
                "value": "Erica Sports Bra",
                "is_filter": True,
            }],
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
        frame_no=1,
    )

    scope = frame.requirement_scopes[requirement.id]
    assert scope["status"] == "met"
    assert scope["query_outcome"] == "empty"
    assert scope["empty_authoritative"] is False
    assert frame.collections == []
    assert frame.missing_requirements == [requirement.id]


def test_complete_candidate_surface_is_validated_without_exact_ui_filter(tmp_path: Path) -> None:
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

    assert before.missing_requirements == []
    assert before.requirement_scopes["filtered_records"]["status"] == "met"
    assert before.requirement_scopes["filtered_records"]["evidence"] == (
        "validated_candidates"
    )
    assert materializer.data_store.collection_rows(before.collections[0].ref) == [{
        "record_id": "2",
        "owner": "b",
        "status": "Complete",
    }]
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
    # Pure ReAct collectors publish assembled detail rows eagerly once resolved;
    # collections may be non-empty here (accumulated data for snapshot on complete).
    # The important state is the resolution marker.
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
    assert materializer.data_store.collection_rows(collection.ref) == case["expected_rows"]


def test_linked_detail_table_expands_filtered_parents_to_child_rows(tmp_path: Path) -> None:
    requirement = DataRequirement(
        id="entries",
        description="Matching child entries in the requested date interval",
        row_schema={
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "recorded_at": {"type": "string", "format": "date-time"},
                "subject": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["record_id", "recorded_at", "subject", "amount"],
            "additionalProperties": False,
        },
        field_sources={
            "record_id": "Record ID",
            "recorded_at": "Date",
            "subject": "Subject",
            "amount": "Amount",
        },
        field_types={
            "record_id": "text",
            "recorded_at": "datetime",
            "subject": "text",
            "amount": "money",
        },
        filters={
            "recorded_at": {
                "min": "2023-01-01T00:00:00+00:00",
                "max": "2023-01-31T23:59:59+00:00",
            },
            "subject_contains": "target class",
        },
    )
    parent_table = {
        "caption": "Records",
        "headers": ["Record ID", "Date", "Action"],
        "rows": [
            {"Record ID": "group-001", "Date": "1/29/23", "Action": "Open"},
            {"Record ID": "group-002", "Date": "1/16/23", "Action": "Open"},
            {"Record ID": "group-003", "Date": "12/18/22", "Action": "Open"},
        ],
        "total_records": 99,
        "partial": True,
        "traversal": {"type": "paged", "has_next_page": True},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._semantic_judge = lambda _requirement, rows: [  # type: ignore[method-assign]
        row for row in rows if row.get("subject") == "Target service"
    ]

    listed, _ = materializer.observe(
        bundle=FakeBundle([parent_table]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )
    assert listed.requirement_scopes[requirement.id]["detail_resolution"][
        "candidate_records"
    ] == 2
    assert materializer._expected_totals == {}

    unrelated_child = {
        "caption": "Entries",
        "headers": ["Subject", "Amount"],
        "rows": [{"Subject": "Unrelated service", "Amount": "$4.00"}],
        "total_records": 200,
        "partial": True,
        "traversal": {"type": "static"},
    }
    first, _ = materializer.observe(
        bundle=FakeBundle([unrelated_child], observation={"title": "Record group-001"}),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )
    progress = first.requirement_scopes[requirement.id]["detail_resolution"]
    assert progress["resolved_candidate_ordinals"] == [1]
    assert progress["next_unresolved_candidate"]["fields"]["record_id"] == "group-002"

    matching_child = {
        **unrelated_child,
        "rows": [
            {"Subject": "Target service", "Amount": "$7.25"},
            {"Subject": "Accessory", "Amount": "$2.00"},
        ],
    }
    completed, _ = materializer.observe(
        bundle=FakeBundle([matching_child], observation={"title": "Record group-002"}),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=3,
    )

    assert completed.requirement_scopes[requirement.id]["detail_resolution"][
        "status"
    ] == "resolved"
    assert materializer.data_store.collection_rows(completed.collections[0].ref) == [{
        "record_id": "group-002",
        "recorded_at": "2023-01-16T00:00:00+00:00",
        "subject": "Target service",
        "amount": 7.25,
    }]


@pytest.mark.parametrize(
    ("subject", "has_previous"),
    [
        ("Target service", False),
        ("Unrelated service", False),
        ("Unrelated service", True),
    ],
)
@pytest.mark.parametrize(
    ("item_field", "item_source"),
    [("item_name", "Item Name"), ("item_description", "Item Description")],
)
def test_singleton_linked_detail_stops_or_continues_source(
    tmp_path: Path,
    subject: str,
    has_previous: bool,
    item_field: str,
    item_source: str,
) -> None:
    requirement = DataRequirement(
        id="entries",
        description="One matching linked entry",
        cardinality="one",
        row_schema={
            "type": "object",
            "properties": {
                field: {"type": "string"} for field in ("record_id", item_field)
            },
            "required": ["record_id", item_field],
            "additionalProperties": False,
        },
        field_sources={"record_id": "Record ID", item_field: item_source},
        field_types={"record_id": "text", item_field: "text"},
        filters={f"{item_field}_contains": "target class"},
    )
    parent = {
        "caption": "Records",
        "headers": ["Record ID", "Action"],
        "rows": [{
            "Record ID": "group-001",
            "Action": "Open",
            "Action_url": "http://example.test/record/1/",
        }],
        "partial": True,
        "traversal": {
            "type": "paged",
            "has_next_page": not has_previous,
            "has_prev_page": has_previous,
        },
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._semantic_judge = lambda _requirement, rows: [  # type: ignore[method-assign]
        row for row in rows if row.get(item_field) == "Target service"
    ]
    source_url = "http://example.test/records/" + ("?p=4" if has_previous else "")
    materializer.observe(
        bundle=FakeBundle([parent], observation={"url": source_url}),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )
    completed, _ = materializer.observe(
        bundle=FakeBundle(
            [{
                "caption": "Entries",
                "headers": ["Product Name"],
                "rows": [{"Product Name": subject}],
                "partial": False,
                "traversal": {"type": "static"},
            }],
            observation={
                "url": "http://example.test/record/1/",
                "title": "Record details",
            },
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )

    scope = completed.requirement_scopes[requirement.id]["detail_resolution"]
    if subject != "Target service":
        assert scope["status"] == "active"
        assert scope["window_exhausted"] is True
        assert scope["candidate_source_url"] == source_url
        assert completed.collections == []
        return
    assert scope["status"] == "resolved"
    assert completed.collections[0].coverage["cardinality"] == "one"
    assert materializer.data_store.collection_rows(
        completed.collections[0].ref
    ) == [{"record_id": "group-001", item_field: "Target service"}]


def _linked_entry_requirement() -> DataRequirement:
    return DataRequirement(
        id="entries",
        description="First matching linked entry",
        cardinality="one",
        row_schema={
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["record_id", "subject"],
            "additionalProperties": False,
        },
        field_sources={"record_id": "Record ID", "subject": "Subject"},
        field_types={"record_id": "text", "subject": "text"},
        filters={"subject_contains": "target class"},
        coverage="first_match",
    )


def _linked_parent_table(caption: str, ids: list[str]) -> dict:
    return {
        "caption": caption,
        "headers": ["Record ID", "Action"],
        "rows": [{
            "Record ID": record_id,
            "Action": "Open",
            "Action_url": f"http://example.test/record/{record_id}/",
        } for record_id in ids],
        "partial": False,
        "traversal": {"type": "static"},
    }


def _linked_entry_materializer(tmp_path: Path) -> PerceptionMaterializer:
    materializer = _materializer(tmp_path, "enhanced")
    materializer._semantic_judge = lambda _requirement, rows: [  # type: ignore[method-assign]
        row for row in rows if row.get("subject") == "Target service"
    ]
    return materializer


def test_rejected_singleton_detail_does_not_complete_with_zero_rows(
    tmp_path: Path,
) -> None:
    requirement = _linked_entry_requirement()
    materializer = _linked_entry_materializer(tmp_path)
    materializer._semantic_judge = lambda _requirement, _rows: []  # type: ignore[method-assign]
    materializer.observe(
        bundle=FakeBundle([_linked_parent_table("Records", ["one"])]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )
    rejected, _ = materializer.observe(
        bundle=FakeBundle([{
            "caption": "Entries",
            "headers": ["Subject"],
            "rows": [{"Subject": "Unrelated service"}],
            "partial": False,
            "traversal": {"type": "static"},
        }], observation={"url": "http://example.test/record/one/"}),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )

    assert rejected.collections == []
    assert rejected.missing_requirements == ["entries"]
    assert rejected.requirement_scopes["entries"]["detail_resolution"]["status"] == "active"


def test_navigable_parent_replaces_residual_detail_candidate(tmp_path: Path) -> None:
    requirement = _linked_entry_requirement()
    materializer = _linked_entry_materializer(tmp_path)
    materializer.observe(
        bundle=FakeBundle([{
            "caption": "Entries",
            "headers": ["Subject"],
            "rows": [{"Subject": "Unrelated service"}],
            "partial": False,
            "traversal": {"type": "static"},
        }], observation={"url": "http://example.test/record/old/"}),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )

    listed, _ = materializer.observe(
        bundle=FakeBundle(
            [_linked_parent_table("Records", ["group-001"])],
            observation={"url": "http://example.test/records/"},
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )

    progress = listed.requirement_scopes[requirement.id]["detail_resolution"]
    assert progress["status"] == "active"
    assert progress["candidate_records"] == 1
    assert progress["resolved_candidate_ordinals"] == []
    assert progress["next_unresolved_candidate"] == {
        "ordinal": 1,
        "fields": {"record_id": "group-001"},
        "navigation_url": "http://example.test/record/group-001/",
    }
    assert listed.collections == []


def test_overlapping_navigable_source_extends_resolved_candidates(tmp_path: Path) -> None:
    requirement = _linked_entry_requirement()
    materializer = _linked_entry_materializer(tmp_path)

    materializer.observe(
        bundle=FakeBundle([_linked_parent_table("Recent Records", ["one"])]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )
    materializer.observe(
        bundle=FakeBundle([{
            "caption": "Entries",
            "headers": ["Subject"],
            "rows": [{"Subject": "Unrelated service"}],
            "partial": False,
            "traversal": {"type": "static"},
        }], observation={"url": "http://example.test/record/one/"}),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )
    extended, _ = materializer.observe(
        bundle=FakeBundle([_linked_parent_table("All Records", ["one", "two"])]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=3,
    )

    progress = extended.requirement_scopes[requirement.id]["detail_resolution"]
    assert progress["candidate_records"] == 2
    assert progress["resolved_candidate_ordinals"] == [1]
    assert progress["next_unresolved_candidate"]["fields"] == {"record_id": "two"}
    assert progress["next_unresolved_candidate"]["navigation_url"].endswith("/two/")


def test_vision_detail_row_advances_unresolved_candidate(tmp_path: Path) -> None:
    """Vision-only detail fields (absent from form controls) must resolve the candidate."""

    requirement = DataRequirement(
        id="items",
        description="Items",
        target_label="Items",
        row_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "owner": {"type": "string"},
                "score": {"type": "number"},
            },
            "required": ["title", "owner", "score"],
            "additionalProperties": False,
        },
        field_sources={"title": "Title", "owner": "Owner", "score": "Score"},
        field_types={"title": "text", "owner": "text", "score": "number"},
    )
    list_table = {
        "headers": ["Title", "Owner", "Action"],
        "rows": [
            {"Title": "Alpha", "Owner": "Ann", "Action": "Edit"},
            {"Title": "Beta", "Owner": "Bob", "Action": "Edit"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    vision_rows = iter([
        [{"title": "Alpha", "owner": "Ann", "score": 1}],
        [{"title": "Beta", "owner": "Bob", "score": 2}],
    ])
    materializer._vision_extract = lambda *_a, **_k: {  # type: ignore[method-assign]
        "found": True,
        "end_visible": True,
        "scope_satisfied": True,
        "rows": next(vision_rows),
    }

    def observe(frame_no: int, *, table=None, controls=None):
        return materializer.observe(
            bundle=FakeBundle([table] if table else [], controls=controls or []),
            platform=FakePlatform(),
            requirements=[requirement],
            frame_no=frame_no,
        )[0]

    listed = observe(1, table=list_table)
    assert listed.requirement_scopes["items"]["detail_resolution"]["next_unresolved_candidate"]["ordinal"] == 1

    first = observe(2, controls=[{"label": "Owner", "kind": "text_input", "value": "Ann"}])
    progress = first.requirement_scopes["items"]["detail_resolution"]
    assert progress["resolved_candidate_ordinals"] == [1]
    assert progress["next_unresolved_candidate"]["ordinal"] == 2
    assert "score" in progress["current_observed_detail_fields"]

    last = observe(3, controls=[{"label": "Owner", "kind": "text_input", "value": "Bob"}])
    progress = last.requirement_scopes["items"]["detail_resolution"]
    assert progress["status"] == "resolved"
    assert last.collections[0].row_count == 2


def test_detail_without_identity_advances_next_unresolved(tmp_path: Path) -> None:
    """A sequential Next walk still resolves when the editor has no identity control."""

    requirement = DataRequirement(
        id="reviews",
        description="Reviews",
        target_label="Reviews",
        row_schema={
            "type": "object",
            "properties": {
                "nickname": {"type": "string"},
                "product": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["nickname", "product", "rating"],
            "additionalProperties": False,
        },
        field_sources={
            "nickname": "Nickname",
            "product": "Product",
            "rating": "Detailed Rating",
        },
        field_types={"nickname": "text", "product": "text", "rating": "number"},
    )
    list_table = {
        "headers": ["Nickname", "Product", "Action"],
        "rows": [
            {"Nickname": "Ann", "Product": "Tank A", "Action": "Edit"},
            {"Nickname": "Bob", "Product": "Tank B", "Action": "Edit"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("identity-free detail pages should use the star control")
    )

    def observe(frame_no: int, *, table=None, controls=None):
        return materializer.observe(
            bundle=FakeBundle([table] if table else [], controls=controls or []),
            platform=FakePlatform(),
            requirements=[requirement],
            frame_no=frame_no,
        )[0]

    observe(1, table=list_table)
    materializer.data_store.put_chunk(
        requirement_id="reviews",
        frame_id="prefix",
        provider="vision",
        rows=[{"nickname": "Ann", "product": "Tank A", "rating": 3}],
        row_schema=requirement.row_schema,
        coverage={
            "scope_status": "met",
            "status": "incomplete",
            "requested_filters": {},
            "collection_key": "visual:prefix",
            "total_records": 2,
            "partial": True,
            "at_end": False,
        },
    )
    first = observe(
        2,
        controls=[{"label": "Detailed Rating", "kind": "number", "value": 3}],
    )
    progress = first.requirement_scopes["reviews"]["detail_resolution"]
    assert progress["resolved_candidate_ordinals"] == [1]
    assert progress["next_unresolved_candidate"]["ordinal"] == 2

    last = observe(
        3,
        controls=[{"label": "Detailed Rating", "kind": "number", "value": 2}],
    )
    progress = last.requirement_scopes["reviews"]["detail_resolution"]
    assert progress["status"] == "resolved"
    assert last.collections[0].row_count == 2
    assert materializer.data_store.collection_rows(last.collections[0].ref) == [
        {"nickname": "Ann", "product": "Tank A", "rating": 3},
        {"nickname": "Bob", "product": "Tank B", "rating": 2},
    ]

    partial_grid = observe(
        4,
        table={
            "headers": ["Nickname", "Product", "Action"],
            "rows": [
                {"Nickname": "Ann", "Product": "Tank A", "Action": "Edit"},
            ],
            "total_records": 2,
            "partial": True,
            "traversal": {"type": "static"},
        },
    )
    assert partial_grid.requirement_scopes["reviews"]["detail_resolution"]["status"] == "resolved"


def test_rating_control_fills_named_editor_without_counting_stars(
    tmp_path: Path,
) -> None:
    """A named editor's rating widget is the selected control value, not a vision star count."""

    requirement = DataRequirement(
        id="reviews",
        description="Reviews",
        target_label="Reviews",
        row_schema={
            "type": "object",
            "properties": {
                "nickname": {"type": "string"},
                "product": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["nickname", "product", "rating"],
            "additionalProperties": False,
        },
        field_sources={
            "nickname": "Nickname",
            "product": "Product",
            "rating": "Detailed Rating",
        },
        field_types={"nickname": "text", "product": "text", "rating": "number"},
    )
    list_table = {
        "headers": ["Nickname", "Product", "Action"],
        "rows": [
            {"Nickname": "Ann", "Product": "Tank A", "Action": "Edit"},
            {"Nickname": "Bob", "Product": "Tank A", "Action": "Edit"},
        ],
        "total_records": 2,
        "partial": False,
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("rating widgets must be read from form controls")
    )

    def observe(frame_no: int, *, table=None, controls=None):
        return materializer.observe(
            bundle=FakeBundle([table] if table else [], controls=controls or []),
            platform=FakePlatform(),
            requirements=[requirement],
            frame_no=frame_no,
        )[0]

    observe(1, table=list_table)
    observe(
        2,
        controls=[
            {"label": "Nickname", "kind": "text_input", "value": "Ann"},
            {"label": "Detailed Rating", "kind": "rating", "value": "2"},
        ],
    )
    last = observe(
        3,
        controls=[
            {"label": "Nickname", "kind": "text_input", "value": "Bob"},
            {"label": "Detailed Rating", "kind": "rating", "value": "5"},
        ],
    )
    assert last.requirement_scopes["reviews"]["detail_resolution"]["status"] == "resolved"
    assert materializer.data_store.collection_rows(last.collections[0].ref) == [
        {"nickname": "Ann", "product": "Tank A", "rating": 2},
        {"nickname": "Bob", "product": "Tank A", "rating": 5},
    ]


def test_rating_control_uses_selected_scale_not_option_id(tmp_path: Path) -> None:
    """Hidden radio widgets often store option ids; the selected scale is 1–N."""

    requirement = DataRequirement(
        id="reviews",
        description="Reviews",
        target_label="Reviews",
        row_schema={
            "type": "object",
            "properties": {
                "nickname": {"type": "string"},
                "product": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["nickname", "product", "rating"],
            "additionalProperties": False,
        },
        field_sources={
            "nickname": "Nickname",
            "product": "Product",
            "rating": "Detailed Rating",
        },
        field_types={"nickname": "text", "product": "text", "rating": "number"},
    )
    list_table = {
        "headers": ["Nickname", "Product", "Action"],
        "rows": [
            {"Nickname": "Ann", "Product": "Tank A", "Action": "Edit"},
        ],
        "total_records": 1,
        "partial": False,
        "traversal": {"type": "static"},
    }
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("rating widgets must be read from form controls")
    )
    materializer.observe(
        bundle=FakeBundle([list_table]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
    )
    done = materializer.observe(
        bundle=FakeBundle([], controls=[{
            "label": "Nickname",
            "kind": "text_input",
            "value": "Ann",
        }, {
            "label": "Detailed Rating",
            "kind": "rating",
            "value": "18",
            "selected_text": "3",
            "selected_text_primary": "3",
        }]),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=2,
    )[0]
    assert done.requirement_scopes["reviews"]["detail_resolution"]["status"] == "resolved"
    assert materializer.data_store.collection_rows(done.collections[0].ref) == [
        {"nickname": "Ann", "product": "Tank A", "rating": 3},
    ]


def _review_requirement(**filters: object) -> DataRequirement:
    return DataRequirement(
        id="reviews",
        description="Reviews",
        target_label="Reviews",
        row_schema={
            "type": "object",
            "properties": {
                "nickname": {"type": "string"},
                "product": {"type": "string"},
                "rating": {"type": "number"},
            },
            "required": ["nickname", "product", "rating"],
            "additionalProperties": False,
        },
        field_sources={
            "nickname": "Nickname",
            "product": "Product",
            "rating": "Detailed Rating",
        },
        field_types={"nickname": "text", "product": "text", "rating": "number"},
        filters=dict(filters),
    )


def _review_table(rows: list[dict[str, str]], *, total: int) -> dict:
    return {
        "headers": ["Nickname", "Product", "Action"],
        "rows": [{**row, "Action": "Edit"} for row in rows],
        "total_records": total,
        "partial": len(rows) < total,
        "traversal": {"type": "static"},
    }


def test_named_stranger_does_not_fill_first_unresolved(tmp_path: Path) -> None:
    """An editor whose identity is not in the candidate set must not credit the first gap."""

    requirement = _review_requirement()
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("named editors should not fall through to vision")
    )

    def observe(frame_no: int, *, table=None, controls=None):
        return materializer.observe(
            bundle=FakeBundle([table] if table else [], controls=controls or []),
            platform=FakePlatform(),
            requirements=[requirement],
            frame_no=frame_no,
        )[0]

    observe(
        1,
        table=_review_table(
            [
                {"Nickname": "Ann", "Product": "Tank A"},
                {"Nickname": "Bob", "Product": "Tank B"},
            ],
            total=2,
        ),
    )
    stranger = observe(
        2,
        controls=[
            {"label": "Nickname", "kind": "text_input", "value": "Cy"},
            {"label": "Detailed Rating", "kind": "rating", "value": "5"},
        ],
    )
    progress = stranger.requirement_scopes["reviews"]["detail_resolution"]
    assert progress["status"] == "active"
    assert progress["resolved_candidate_ordinals"] == []
    assert progress["next_unresolved_candidate"]["ordinal"] == 1
    assert stranger.collections == []


def test_named_stranger_does_not_fill_pending_candidate(tmp_path: Path) -> None:
    """An explicit mismatch must not be credited to the row with an empty detail."""

    requirement = _review_requirement()
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("named editors should not fall through to vision")
    )

    def observe(frame_no: int, *, table=None, controls=None):
        return materializer.observe(
            bundle=FakeBundle([table] if table else [], controls=controls or []),
            platform=FakePlatform(),
            requirements=[requirement],
            frame_no=frame_no,
        )[0]

    observe(
        1,
        table=_review_table(
            [
                {"Nickname": "Ann", "Product": "Tank A"},
                {"Nickname": "Bob", "Product": "Tank B"},
            ],
            total=2,
        ),
    )
    pending = observe(
        2,
        controls=[
            {"label": "Nickname", "kind": "text_input", "value": "Ann"},
            {"label": "Detailed Rating", "kind": "rating", "value": ""},
        ],
    )
    assert pending.requirement_scopes["reviews"]["detail_resolution"][
        "pending_candidate_ordinal"
    ] == 1

    stranger = observe(
        3,
        controls=[
            {"label": "Nickname", "kind": "text_input", "value": "Cy"},
            {"label": "Product", "kind": "text_input", "value": "Tank A"},
            {"label": "Detailed Rating", "kind": "rating", "value": "5"},
        ],
    )
    progress = stranger.requirement_scopes["reviews"]["detail_resolution"]
    assert progress["pending_candidate_ordinal"] == 1
    assert progress["resolved_candidate_ordinals"] == []
    assert progress["next_unresolved_candidate"]["fields"]["nickname"] == "Ann"
    assert stranger.collections == []


def test_unresolved_candidates_accumulate_across_same_scope_pages(tmp_path: Path) -> None:
    """A later window must extend the candidate set before any detail is resolved."""

    requirement = _review_requirement()
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("structured pagination should not invoke visual extraction")
    )

    def observe(frame_no: int, rows: list[dict[str, str]]):
        return materializer.observe(
            bundle=FakeBundle([_review_table(rows, total=3)]),
            platform=FakePlatform(),
            requirements=[requirement],
            frame_no=frame_no,
        )[0]

    observe(
        1,
        [
            {"Nickname": "Ann", "Product": "Tank A"},
            {"Nickname": "Bob", "Product": "Tank B"},
        ],
    )
    later = observe(2, [{"Nickname": "Cy", "Product": "Tank C"}])

    progress = later.requirement_scopes["reviews"]["detail_resolution"]
    assert progress["candidate_records"] == 3
    assert progress["resolved_candidate_ordinals"] == []
    assert progress["next_unresolved_candidate"]["fields"]["nickname"] == "Ann"
    assert later.collections == []


def test_acquisition_scope_change_resets_detail_collection(tmp_path: Path) -> None:
    """A new physical query must not union rows assembled under a prior acquisition scope."""

    requirement = _review_requirement(product="Tank A")
    materializer = _materializer(tmp_path, "enhanced")
    materializer._vision_extract = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("scope reset should keep using structured list/detail evidence")
    )

    def observe(frame_no: int, *, product: str, table=None, controls=None):
        scoped = requirement.model_copy(update={"filters": {"product": product}})
        return materializer.observe(
            bundle=FakeBundle(
                [table] if table else [],
                controls=controls or [],
                applied_filters={"Product": product},
                applied_filter_state=AppliedFilterState(
                    predicates=compile_filter_predicates({"Product": product}),
                    coverage="complete",
                    source="replay",
                ),
            ),
            platform=FakePlatform(),
            requirements=[scoped],
            frame_no=frame_no,
        )[0]

    observe(
        1,
        product="Tank A",
        table=_review_table(
            [
                {"Nickname": "Ann", "Product": "Tank A"},
                {"Nickname": "Bob", "Product": "Tank A"},
            ],
            total=2,
        ),
    )
    observe(
        2,
        product="Tank A",
        controls=[
            {"label": "Nickname", "kind": "text_input", "value": "Ann"},
            {"label": "Detailed Rating", "kind": "rating", "value": "2"},
        ],
    )
    changed = observe(
        3,
        product="Tank B",
        table=_review_table(
            [
                {"Nickname": "Cy", "Product": "Tank B"},
                {"Nickname": "Dee", "Product": "Tank B"},
            ],
            total=2,
        ),
    )
    progress = changed.requirement_scopes["reviews"]["detail_resolution"]
    assert progress["candidate_records"] == 2
    assert progress["resolved_candidate_ordinals"] == []
    assert progress["next_unresolved_candidate"]["fields"]["nickname"] == "Cy"
    assert changed.collections == []


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


def test_confirmed_exact_filter_supplies_exact_scope_fields(tmp_path: Path) -> None:
    requirement = _requirement().model_copy(update={"filters": {"term": "fixed"}})
    assert set(_visible_row_schema(requirement, {})["properties"]) == {"term", "uses"}
    materializer = _materializer(tmp_path, "vision-only")
    materializer.mode = "enhanced"
    materializer._vision_extract = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "found": True,
        "rows": [{"uses": 20}],
        "filter_state_visible": True,
        "start_visible": True,
        "end_visible": True,
    }
    frame, _ = materializer.observe(
        bundle=FakeBundle(
            [],
            applied_filters={"Search Term": "fixed"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({"Search Term": "fixed"}),
                coverage="complete",
                source="filter_indicator",
            ),
        ),
        platform=FakePlatform(),
        requirements=[requirement],
        frame_no=1,
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
