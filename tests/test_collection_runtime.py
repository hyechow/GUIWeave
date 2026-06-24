from gui_agent.core.orchestrator.collection_runtime import CollectionRuntime
from gui_agent.core.schemas import Observation


def _obs(*, tables=None, controls=None) -> Observation:
    return Observation(
        png_bytes=b"",
        source="test",
        tables=tables,
        form_controls=controls,
    )


def _table(rows, *, total=None, page=1, has_next=False):
    return {
        "headers": ["Code", "Name", "Action"],
        "rows": rows,
        "total_records": total,
        "traversal": {
            "type": "paged",
            "page_index": page,
            "has_next_page": has_next,
            "has_prev_page": page > 1,
        },
    }


def test_direct_table_collection_pages_until_total():
    runtime = CollectionRuntime(var="items", returns=["Code", "Name"])

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
        ]),
        read_detail=lambda fields: {},
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
        ]),
        read_detail=lambda fields: {},
    )

    assert second.action == "done"
    assert runtime.rows == [
        {"Code": "A1", "Name": "Alpha"},
        {"Code": "B2", "Name": "Beta"},
        {"Code": "C3", "Name": "Gamma"},
    ]


def test_detail_collection_walks_rows_and_returns_to_list():
    runtime = CollectionRuntime(var="items", returns=["Code", "Name", "Detail Field"])
    table = _table(
        [
            {"Code": "A1", "Name": "Alpha", "Action": "Edit"},
            {"Code": "B2", "Name": "Beta", "Action": "Edit"},
        ],
        total=2,
        page=1,
        has_next=False,
    )

    first = runtime.update(_obs(tables=[table]), read_detail=lambda fields: {})
    assert first.action == "open_row"
    assert "第 1 行" in first.instruction

    detail_done = runtime.update(
        _obs(controls=[{"label": "Detail Field", "selected_text": "One"}]),
        read_detail=lambda fields: {},
    )
    assert detail_done.action == "return_to_list"
    assert runtime.rows == [{"Code": "A1", "Name": "Alpha", "Detail Field": "One"}]

    next_row = runtime.update(_obs(tables=[table]), read_detail=lambda fields: {})
    assert next_row.action == "open_row"
    assert "第 2 行" in next_row.instruction

    runtime.update(
        _obs(controls=[{"label": "Detail Field", "selected_text": "Two"}]),
        read_detail=lambda fields: {},
    )
    done = runtime.update(_obs(tables=[table]), read_detail=lambda fields: {})

    assert done.action == "done"
    assert runtime.rows == [
        {"Code": "A1", "Name": "Alpha", "Detail Field": "One"},
        {"Code": "B2", "Name": "Beta", "Detail Field": "Two"},
    ]


def test_detail_collection_falls_back_to_reader_when_controls_do_not_cover_field():
    runtime = CollectionRuntime(var="items", returns=["Code", "Detail Field"])
    table = _table(
        [{"Code": "A1", "Name": "Alpha", "Action": "Edit"}],
        total=1,
        page=1,
        has_next=False,
    )
    runtime.update(_obs(tables=[table]), read_detail=lambda fields: {})

    calls: list[list[str]] = []

    def read_detail(fields: list[str]) -> dict[str, str]:
        calls.append(fields)
        return {"Detail Field": "Read by vision"}

    decision = runtime.update(_obs(), read_detail=read_detail)

    assert decision.action == "return_to_list"
    assert calls == [["Detail Field"]]
    assert runtime.rows == [{"Code": "A1", "Detail Field": "Read by vision"}]


def test_returned_list_does_not_require_completed_row_to_still_be_visible():
    runtime = CollectionRuntime(var="items", returns=["Code", "Detail Field"])
    first_table = _table(
        [{"Code": "A1", "Name": "Alpha", "Action": "Edit"}],
        total=2,
        page=1,
        has_next=False,
    )
    runtime.update(_obs(tables=[first_table]), read_detail=lambda fields: {})
    runtime.update(
        _obs(controls=[{"label": "Detail Field", "selected_text": "One"}]),
        read_detail=lambda fields: {},
    )

    changed_visible_rows = _table(
        [{"Code": "B2", "Name": "Beta", "Action": "Edit"}],
        total=2,
        page=1,
        has_next=False,
    )
    decision = runtime.update(_obs(tables=[changed_visible_rows]), read_detail=lambda fields: {})

    assert decision.action == "open_row"
    assert "B2" in decision.instruction
    assert runtime.rows == [{"Code": "A1", "Detail Field": "One"}]
