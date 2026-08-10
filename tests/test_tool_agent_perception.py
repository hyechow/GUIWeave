from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from gui_agent.core.filter_contract import AppliedFilterState, compile_filter_predicates
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
        applied_filters: dict[str, str] | None = None,
        applied_filter_state: AppliedFilterState | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state

    def observe(self):
        return SimpleNamespace(
            png_bytes=b"png",
            tables=self.tables,
            form_controls=self.controls,
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
        applied_filters: dict[str, str] | None = None,
        applied_filter_state: AppliedFilterState | None = None,
    ) -> None:
        self.tables = tables
        self.controls = controls or []
        self.applied_filters = applied_filters or {}
        self.applied_filter_state = applied_filter_state
        self.make_perception_calls = 0

    def make_perception(self, _platform, _path):
        self.make_perception_calls += 1
        return FakePerception(
            self.tables,
            controls=self.controls,
            applied_filters=self.applied_filters,
            applied_filter_state=self.applied_filter_state,
        )


def _materializer(tmp_path: Path, mode: str) -> PerceptionMaterializer:
    materializer = PerceptionMaterializer.__new__(PerceptionMaterializer)
    materializer.mode = mode
    materializer.data_store = RuntimeDataStore()
    materializer.log_dir = tmp_path
    materializer.model = "fake"
    materializer._vision_extract = lambda _requirement, _png: {  # type: ignore[method-assign]
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
    materializer._vision_extract = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
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
    materializer._vision_extract = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
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
    materializer._vision_extract = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
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


def test_vision_only_never_invokes_platform_perception(tmp_path: Path) -> None:
    bundle = FakeBundle([
        {
            "caption": "Top Terms",
            "in_viewport": True,
            "rows": [{"Search Term": "hidden-dom-value", "Uses": "99"}],
        }
    ])
    materializer = _materializer(tmp_path, "vision-only")
    materializer._vision_extract = lambda _requirement, _png: {  # type: ignore[method-assign]
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
