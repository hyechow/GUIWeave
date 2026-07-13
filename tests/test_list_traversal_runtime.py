from gui_agent.core.orchestrator.traversal.list_runtime import ListTraversalRuntime
from gui_agent.core.schemas import Observation


def _obs(*, tables=None, controls=None, viewport=None, png_bytes=b"") -> Observation:
    return Observation(
        png_bytes=png_bytes,
        source="test",
        tables=tables,
        form_controls=controls,
        viewport=viewport,
    )


def _table(rows, *, total=None, page=1, has_next=False, traversal_extra=None):
    traversal = {
        "type": "paged",
        "page_index": page,
        "has_next_page": has_next,
        "has_prev_page": page > 1,
    }
    if traversal_extra:
        traversal.update(traversal_extra)
    return {
        "headers": ["Code", "Name", "Action"],
        "rows": rows,
        "total_records": total,
        "traversal": traversal,
    }


def test_direct_table_collection_pages_until_total():
    runtime = ListTraversalRuntime(var="items", returns=["Code", "Name"])

    first = runtime.update(
        _obs(tables=[
            _table(
                [
                    {"Code": "A1", "Name": "Alpha", "Action": "Edit"},
                    {"Code": "B2", "Name": "Beta", "Action": "Edit"},
                ],
                total=3,
                page=1,
                has_next=True,
            )
        ])
    )

    assert first.action == "paginate_next"
    assert runtime.rows == [
        {"Code": "A1", "Name": "Alpha"},
        {"Code": "B2", "Name": "Beta"},
    ]

    second = runtime.update(
        _obs(tables=[
            _table(
                [{"Code": "C3", "Name": "Gamma", "Action": "Edit"}],
                total=3,
                page=2,
                has_next=False,
            )
        ])
    )

    assert second.action == "done"
    assert runtime.rows == [
        {"Code": "A1", "Name": "Alpha"},
        {"Code": "B2", "Name": "Beta"},
        {"Code": "C3", "Name": "Gamma"},
    ]


def test_missing_detail_fields_are_accumulated_without_opening_rows():
    runtime = ListTraversalRuntime(var="items", returns=["Code", "Name", "Detail Field"])
    table = _table(
        [
            {"Code": "A1", "Name": "Alpha", "Action": "Edit"},
            {"Code": "B2", "Name": "Beta", "Action": "Edit"},
        ],
        total=2,
        page=1,
        has_next=False,
    )

    decision = runtime.update(_obs(tables=[table]))

    assert decision.action == "done"
    assert runtime.rows == [
        {"Code": "A1", "Name": "Alpha", "Detail Field": ""},
        {"Code": "B2", "Name": "Beta", "Detail Field": ""},
    ]


def test_page_size_is_only_metadata_and_does_not_change_traversal_correctness():
    """Changing page size is an optional optimization, not part of traversal correctness."""
    runtime = ListTraversalRuntime(var="items", returns=["Code"])
    table = _table(
        [{"Code": "A1", "Name": "Alpha", "Action": "Edit"}],
        total=50,
        page=1,
        has_next=True,
        traversal_extra={
            "page_size": 20,
            "page_size_options": [20, 50, 100, 200],
            "has_page_size_control": True,
            "page_size_menu_open": False,
        },
    )

    decision = runtime.update(_obs(tables=[table]))
    assert decision.action == "paginate_next"


def test_no_page_size_control_skips_straight_to_pagination():
    runtime = ListTraversalRuntime(var="items", returns=["Code"])
    table = _table(
        [{"Code": "A1", "Name": "Alpha", "Action": "Edit"}],
        total=2,
        page=1,
        has_next=True,
    )

    decision = runtime.update(_obs(tables=[table]))
    assert decision.action == "paginate_next"


def test_falls_back_when_no_collection_table_is_visible():
    runtime = ListTraversalRuntime(var="items", returns=["Code", "Detail Field"])

    decision = runtime.update(_obs())

    assert decision.action == "fallback"
    assert runtime.rows == []


def test_traversal_decision_works_without_any_table():
    """The view window's viewport signal drives paginate/scroll decisions even when no table
    is ever found — e.g. a card/feed collection, or a vision-only platform. Traversal must not
    require a resolvable table."""
    runtime = ListTraversalRuntime(var="items", returns=["Code"])

    first = runtime.update(_obs(viewport={"type": "scroll", "can_scroll_more": True, "at_scroll_end": False}))
    assert first.action == "scroll_down"
    assert runtime.rows == []

    second = runtime.update(_obs(viewport={"type": "scroll", "can_scroll_more": False, "at_scroll_end": True}))
    assert second.action == "done"


def test_pixel_freeze_fallback_when_no_viewport_signal_at_all():
    """iPhone/Android (and any browser page where the page-level sensor found nothing) have no
    Observation.viewport. Traversal should still deterministically detect a frozen screen via
    consecutive near-identical frames, instead of looping on 'fallback' forever."""
    import io

    from PIL import Image

    def _png(color: tuple[int, int, int]) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), color).save(buf, format="PNG")
        return buf.getvalue()

    runtime = ListTraversalRuntime(var="items", returns=["Code"])
    frozen_frame = _png((10, 20, 30))

    decisions = [runtime.update(_obs(png_bytes=frozen_frame)) for _ in range(4)]

    # The first frame starts a forward search. Repeated frames are execution evidence that the
    # same bound visual surface did not move, so the shared traversal session terminates.
    assert decisions[0].action == "scroll_down"
    assert decisions[-1].action == "done"


def test_visible_row_changes_are_accumulated_by_key_without_detail_state():
    runtime = ListTraversalRuntime(var="items", returns=["Code", "Detail Field"])
    first_table = _table(
        [{"Code": "A1", "Name": "Alpha", "Action": "Edit"}],
        total=2,
        page=1,
        has_next=False,
    )
    runtime.update(_obs(tables=[first_table]))

    changed_visible_rows = _table(
        [{"Code": "B2", "Name": "Beta", "Action": "Edit"}],
        total=2,
        page=1,
        has_next=False,
    )
    decision = runtime.update(_obs(tables=[changed_visible_rows]))

    assert decision.action == "done"
    assert runtime.rows == [
        {"Code": "A1", "Detail Field": ""},
        {"Code": "B2", "Detail Field": ""},
    ]


def test_does_not_finish_when_declared_total_is_not_reached():
    runtime = ListTraversalRuntime(var="items", returns=["Code"])
    first_table = _table(
        [{"Code": f"A{i}", "Name": f"Alpha {i}", "Action": "Edit"} for i in range(6)],
        total=7,
        page=1,
        has_next=True,
    )
    first = runtime.update(_obs(tables=[first_table]))
    assert first.action == "paginate_next"

    duplicate_last_page = _table(
        [{"Code": "A1", "Name": "Alpha 1", "Action": "Edit"}],
        total=7,
        page=2,
        has_next=False,
    )
    decision = runtime.update(_obs(tables=[duplicate_last_page]))

    assert decision.action == "done"
    assert "不一致" in decision.reason
    assert len(runtime.rows) == 6


def test_total_discrepancy_does_not_drive_page_size_change():
    runtime = ListTraversalRuntime(var="items", returns=["Code"])
    first_table = _table(
        [{"Code": f"A{i}", "Name": f"Alpha {i}", "Action": "Edit"} for i in range(6)],
        total=7,
        page=1,
        has_next=True,
        traversal_extra={
            "page_size": 6,
            "page_size_options": [6, 20, 30, 50],
            "has_page_size_control": True,
            "page_size_menu_open": False,
        },
    )
    first = runtime.update(_obs(tables=[first_table]))
    assert first.action == "paginate_next"

    duplicate_last_page = _table(
        [{"Code": "A1", "Name": "Alpha 1", "Action": "Edit"}],
        total=7,
        page=2,
        has_next=False,
        traversal_extra={
            "page_size": 6,
            "page_size_options": [6, 20, 30, 50],
            "has_page_size_control": True,
            "page_size_menu_open": False,
        },
    )
    decision = runtime.update(_obs(tables=[duplicate_last_page]))

    assert decision.action == "done"
    assert "不一致" in decision.reason
    assert "每页条数" in decision.instruction
    assert len(runtime.rows) == 6


def test_page_size_menu_open_does_not_override_boundary_completion():
    runtime = ListTraversalRuntime(var="items", returns=["Code"])
    runtime.update(
        _obs(tables=[
            _table(
                [{"Code": f"A{i}", "Name": f"Alpha {i}", "Action": "Edit"} for i in range(6)],
                total=7,
                page=1,
                has_next=True,
                traversal_extra={"page_size": 6, "page_size_options": [6, 20], "page_size_menu_open": False},
            )
        ])
    )

    decision = runtime.update(
        _obs(tables=[
            _table(
                [{"Code": "A1", "Name": "Alpha 1", "Action": "Edit"}],
                total=7,
                page=2,
                has_next=False,
                traversal_extra={"page_size": 6, "page_size_options": [6, 20], "page_size_menu_open": True},
            )
        ])
    )

    assert decision.action == "done"
    assert "不一致" in decision.reason


def test_resolves_requested_id_suffix_to_unique_id_column():
    runtime = ListTraversalRuntime(
        var="reviews",
        returns=["review_id"],
        read_spec="review_id：逐行读取列表中每条评论行的 ID 列数值，每行一个对象。",
    )
    decision = runtime.update(
        _obs(tables=[
            {
                "headers": ["ID", "Title", "Action"],
                "rows": [
                    {"ID": "351", "Title": "A", "Action": "Edit"},
                    {"ID": "347", "Title": "B", "Action": "Edit"},
                ],
                "total_records": 2,
                "traversal": {
                    "type": "paged",
                    "page_index": 1,
                    "has_next_page": False,
                    "has_prev_page": False,
                },
            }
        ])
    )

    assert decision.action == "done"
    assert runtime.rows == [{"review_id": "351"}, {"review_id": "347"}]


def test_ambiguous_id_columns_stop_collection_instead_of_guessing():
    runtime = ListTraversalRuntime(var="records", returns=["review_id"])
    decision = runtime.update(
        _obs(tables=[
            {
                "headers": ["ID", "Record ID", "Title", "Action"],
                "rows": [{"ID": "351", "Record ID": "R-1", "Title": "A", "Action": "Edit"}],
                "total_records": 1,
                "traversal": {
                    "type": "paged",
                    "page_index": 1,
                    "has_next_page": False,
                    "has_prev_page": False,
                },
            }
        ])
    )

    assert decision.action == "schema_mismatch"
    assert runtime.rows == []


def test_end_boundary_with_low_confidence_total_discrepancy_finishes_with_rows():
    runtime = ListTraversalRuntime(var="reviews", returns=["review_id"])
    decision = runtime.update(
        _obs(tables=[
            {
                "headers": ["ID", "Product", "Action"],
                "rows": [
                    {"ID": "351", "Product": "Olivia 1/4 Zip Light Jacket", "Action": "Edit"},
                    {"ID": "347", "Product": "Olivia 1/4 Zip Light Jacket", "Action": "Edit"},
                    {"ID": "349", "Product": "Olivia 1/4 Zip Light Jacket", "Action": "Edit"},
                ],
                "total_records": 4,
                "traversal": {
                    "type": "paged",
                    "page_index": 1,
                    "page_count": 1,
                    "has_next_page": False,
                    "has_prev_page": False,
                    "page_size": 200,
                    "page_size_options": [20, 30, 50, 100, 200],
                },
            }
        ])
    )

    assert decision.action == "done"
    assert "不一致" in decision.reason
    assert runtime.rows == [{"review_id": "351"}, {"review_id": "347"}, {"review_id": "349"}]
