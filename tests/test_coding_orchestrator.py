from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

import gui_agent.core.orchestrator.sandbox as coding_sandbox
from gui_agent.core.orchestrator import (
    CodingCompileError,
    CodingProgram,
    CodingProgramRuntime,
    FixtureSpec,
    generate_code,
    program_from_plan,
)
from gui_agent.core.orchestrator import planner as coding_planner
from gui_agent.core.orchestrator.planner import (
    _unstructured_visual_diagnostics,
)
from gui_agent.core.orchestrator.sandbox import (
    build_probe_fixture,
    execute_code,
    repair_direct_read_fields,
    validate_code,
    validate_fixture_contract,
    validate_projection_contract,
    validate_runtime_dataflow,
)
from gui_agent.core.run.contracts import Acquire, Command, Interact, Read
from gui_agent.core.router.intent import IntentResolution
from gui_agent.core.schemas import (
    CollectionIntent,
    StatementOutcome,
)


GOOD_PROGRAM = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open the Sahara leggings collection",
        success={
            "entity": "Sahara leggings",
            "fields": ["id", "Name"],
        },
    )
    scope = ctx.query(state, entity="Sahara leggings",
        filters={"Name": "Sahara leggings"},
    )
    products = ctx.acquire(scope, fields=["id", "Name"])
    assert products, "Sahara products are required"
    updates = []
    for product in products:
        detail = ctx.read(state, target=product, fields=["Price"])
        new_price = round(detail["Price"] * 0.8, 2)
        assert new_price < detail["Price"], "price must decrease"
        updates.append([product, new_price])
    for product, new_price in updates:
        state = ctx.reach(
            state,
            "Open the exact product",
            target=product,
            success={
                "entity": "Product",
                "id": product["id"],
                "Name": product["Name"],
            },
        )
        state = ctx.commit(
            state,
            "Update the product price",
            target=product,
            values={"Price": new_price},
        )
    return len(products)
"""


def _fixture() -> FixtureSpec:
    return FixtureSpec(
        lookups={
            "sahara leggings": [{"id": "p1", "Name": "Sahara leggings"}],
            "sahara": [{"id": "p1", "Name": "Sahara leggings"}],
        },
        reads={"p1": {"Price": 100.0}},
    )


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(content=content, usage_metadata={})


class _SequenceLLM:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _response(str(item))


def test_validate_code_accepts_normal_python_client_program() -> None:
    assert validate_code(GOOD_PROGRAM) == []


def test_validate_code_accepts_deterministic_set_deduplication() -> None:
    source = """
def run(ctx):
    values = set()
    values.add("Cotton")
    return sorted(values)
"""

    assert validate_code(source) == []


def test_validate_code_accepts_structured_terminal_reach() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Show the requested report",
        success={
            "entity": "Sales Reports",
            "report": "Orders",
            "from": "05/01/2021",
            "to": "03/31/2022",
            "rendered": True,
        },
    )
"""

    assert validate_code(source) == []
    result = execute_code(source, build_probe_fixture(source))
    assert result.ok, result.error
    assert result.return_value is None
    assert result.trace[0].op == "reach"
    assert result.trace[0].result is None


@pytest.mark.parametrize(
    "method",
    ["gui", "write", "lookup", "interact", "compute"],
)
def test_validate_code_rejects_removed_planning_api(method: str) -> None:
    source = f"def run(ctx):\n    ctx.{method}('x')\n    assert ctx, 'runtime exists'"

    diagnostics = validate_code(source)

    assert any(item.code == "UNKNOWN_CTX_API" for item in diagnostics)


def test_validate_code_accepts_acquire_as_public_api() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open X", success={"entity": "X", "fields": ["id"]})
    scope = ctx.query(state, entity="X")
    return ctx.acquire(scope, fields=["id"])
"""

    assert validate_code(source) == []


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("def run(ctx):\n    open('x')\n    assert ctx, 'runtime exists'", "UNSAFE_CALL"),
        ("def run(ctx):\n    ctx.query('x')\n    assert ctx, 'runtime exists'", "CTX_SIGNATURE"),
        (
            "def run(ctx):\n"
            "    ctx.commit('save', values={'tags': {'a'}})\n"
            "    assert ctx, 'runtime exists'",
            "CTX_JSON_VALUE",
        ),
        (
            "def run(ctx):\n"
            "    ctx.reach('open', values={'Status': 'Complete'})\n"
            "    assert ctx, 'runtime exists'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n"
            "    ctx.reach('open')\n"
            "    assert ctx, 'runtime exists'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n"
            "    success = {'entity': 'Records', 'fields': ['ID']}\n"
            "    ctx.reach('open records', success=success)\n"
            "    assert ctx, 'runtime exists'",
            "REACH_SUCCESS_CONTRACT",
        ),
        (
            "def run(ctx):\n"
            "    ctx.reach('open records', success={'kind': 'done', 'name': 'Records'})\n"
            "    assert ctx, 'runtime exists'",
            "REACH_SUCCESS_CONTRACT",
        ),
        (
            "def run(ctx):\n"
            "    ctx.reach('open', success={'entity': 'Records'})\n"
            "    ctx.read(fields={'Options': 'list'})",
            "FIELD_PROJECTION_CONTRACT",
        ),
        ("def run(ctx):\n    assert True, 'always'", "BUSINESS_ASSERTION_CONSTANT"),
    ],
)
def test_validate_code_rejects_unsafe_or_invalid_source(source: str, code: str) -> None:
    assert any(item.code == code for item in validate_code(source))


def test_validate_code_accepts_reach_and_commit_as_value_returning() -> None:
    direct = """
def run(ctx, state):
    return ctx.commit(state, "Save form", values={"Status": "Complete"})
"""

    assert validate_code(direct) == []


def test_validate_code_requires_state_for_reach_and_commit() -> None:
    assert any(
        item.code == "STATE_REQUIRED"
        for item in validate_code(
            "def run(ctx):\n"
            "    return ctx.commit('save', values={'Status': 'Complete'})"
        )
    )
    assert any(
        item.code == "CTX_SIGNATURE"
        for item in validate_code(
            "def run(ctx):\n"
            "    state = ctx.reach('open', success={'entity': 'Records'})\n"
            "    return state"
        )
    )


def test_validate_code_accepts_local_helpers_and_safe_imports() -> None:
    source = """
from datetime import datetime

def run(ctx, state):
    def newest(rows):
        return sorted(rows, key=lambda row: datetime.fromisoformat(row["Date"]), reverse=True)

    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Date"],
        },
    )
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope, fields=["Date"])
    assert rows, "orders are required"
    return newest(rows)[0]["Date"]
"""

    assert validate_code(source) == []


def test_validate_code_allows_runtime_values_in_literal_reach_success() -> None:
    source = """
def run(ctx, state):
    start = "05/01/2021"
    state = ctx.reach(
        state,
        "Show the requested report",
        success={
            "entity": "Sales Report",
            "conditions": {"From": start, "rendered": True},
        },
    )
"""

    assert validate_code(source) == []


def test_validate_code_allows_unassigned_terminal_reach() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Show the requested report",
        success={"entity": "Sales Report", "rendered": True},
    )
"""

    assert validate_code(source) == []


def test_validate_code_allows_filter_fields_outside_return_projection() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open records", success={
        "entity": "Records", "fields": ["Name"],
    })
    scope = ctx.query(state, entity="Records",
        filters={"Status": "Complete"},
    )
    return ctx.acquire(scope, fields=["Name"])
"""

    assert validate_code(source) == []


def test_verified_reach_state_with_extra_conditions_remains_composable() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open the filtered records view",
        success={
            "entity": "Records",
            "view": "Filtered records",
        },
    )
    scope = ctx.query(state, entity="Records",
        filters={"Status": "Complete"},
    )
    return ctx.acquire(scope, fields=["Name"])
"""

    assert validate_code(source) == []
    result = execute_code(source, build_probe_fixture(source))
    assert result.ok, result.error


def test_reach_state_conditions_do_not_become_compile_time_query_strategy() -> None:
    source = """
def run(ctx, state):
    start = "01/01/2023"
    state = ctx.reach(
        state,
        "Open Orders",
        success={
            "entity": "Orders",
            "Purchase Date from": start,
            "Status": "Complete",
        },
    )
    scope = ctx.query(state, entity="Orders",
        filters={"Status": "Complete"},
    )
    return ctx.acquire(scope, fields={"Purchase Date": "datetime"})
"""

    assert validate_code(source) == []


def test_validate_code_rejects_nested_reach_filters_before_query() -> None:
    source = """
def run(ctx):
    ctx.reach("Open filtered orders", success={
        "entity": "Orders",
        "filters": {"Purchase Date": {"from": "01/01/2023", "to": "05/31/2023"}},
    })
    return ctx.query(
        entity="Orders",
        fields={"Purchase Date": "datetime"},
        filters={"Status": "Complete"},
    )
"""

    diagnostics = validate_code(source)

    assert any(item.code == "QUERY_FILTERS_IN_REACH" for item in diagnostics)


def test_validate_code_rejects_query_row_effects_in_reach_success() -> None:
    source = """
def run(ctx):
    ctx.reach(
        "View matching records",
        success={
            "entity": "Record",
            "scope": "requested",
            "bookmarked": True,
            "favorited": True,
            "rendered": True,
        },
    )
    rows = ctx.query(entity="Record",
        fields={"id": "text", "favorited": "boolean", "bookmarked": "boolean"},
        filters={"scope": "requested"},
    )
    selected = [
        row for row in rows
        if not row["favorited"] and not row.get("bookmarked", False)
    ]
    for row in selected:
        ctx.reach(
            "Open the exact record",
            target=row,
            success={
                "entity": "Record",
                "id": row["id"],
                "favorited": row["favorited"],
                "bookmarked": row["bookmarked"],
            },
        )
        ctx.commit("Update record", target=row, values={"favorited": True})
    return len(selected)
"""

    diagnostics = validate_code(source)

    assert any(
        item.code == "PREMATURE_MUTATION_POSTCONDITION"
        and "favorited" in item.message
        for item in diagnostics
    )
    assert any(
        item.code == "ROW_FIELD_LEAKED_INTO_REACH"
        and "bookmarked" in item.message
        for item in diagnostics
    )


def test_validate_code_keeps_target_identity_out_of_row_field_leakage() -> None:
    source = '''
def run(ctx):
    ctx.reach("Downloads", success={"entity": "Files", "fields": ["name"]})
    rows = ctx.query(entity="Files", fields={"name": "text"})
    target = rows[0]
    ctx.reach(
        "Open archive",
        target=target,
        success={"entity": "ArchiveEntries", "name": target["name"], "fields": ["name"]},
    )
    entries = ctx.query(entity="ArchiveEntries", fields={"name": "text"})
    selected = [entry for entry in entries if entry["name"]]
    ctx.commit(
        "Extract archive",
        target=target,
        values={"selection": "all", "destination": "Downloads"},
    )
    return len(selected)
'''

    diagnostics = validate_code(source)

    assert not any(
        item.code == "ROW_FIELD_LEAKED_INTO_REACH"
        and "name" in item.message
        for item in diagnostics
    )


def test_validate_code_rejects_committed_reach_field_without_projection() -> None:
    source = """
def run(ctx):
    ctx.reach(
        "View matching records",
        success={"entity": "Record", "saved": False},
    )
    rows = ctx.query(entity="Record",
        fields={"id": "text", "content": "text"},
        filters={"saved": False},
    )
    for row in rows:
        ctx.commit("Save record", target=row, values={"saved": True})
"""

    diagnostics = validate_code(source)

    assert any(
        item.code == "PREMATURE_MUTATION_POSTCONDITION"
        and "saved" in item.message
        for item in diagnostics
    )


def test_validate_code_allows_reach_scope_before_query_and_commit() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "View matching records",
        success={"entity": "Record", "scope": "requested", "rendered": True},
    )
    scope = ctx.query(state, entity="Record",
        filters={"scope": "requested"},
    )
    rows = ctx.acquire(scope,
        fields={"id": "text", "favorited": "boolean", "bookmarked": "boolean"},
    )
    selected = [
        row for row in rows
        if not row["favorited"] and not row.get("bookmarked", False)
    ]
    for row in selected:
        state = ctx.reach(
            state,
            "Open the exact record",
            target=row,
            success={
                "entity": "Record",
                "id": row["id"],
                "favorited": row["favorited"],
                "bookmarked": row["bookmarked"],
            },
        )
        state = ctx.commit(state, "Update record", target=row, values={"favorited": True})
    return len(selected)
"""

    assert validate_code(source) == []


def test_validate_code_rejects_commit_after_target_source_is_replaced() -> None:
    source = """
def run(ctx):
    ctx.reach("Open tagged records", success={"entity": "TaggedRecords"})
    tagged = ctx.query(entity="TaggedRecords",
        fields=["author_handle", "content"],
    )
    ctx.reach("Open saved favorites", success={"entity": "SavedFavorites"})
    favorites = ctx.query(entity="SavedFavorites",
        fields=["author_handle", "content"],
    )
    ctx.reach("Open saved bookmarks", success={"entity": "SavedBookmarks"})
    bookmarks = ctx.query(entity="SavedBookmarks",
        fields=["author_handle", "content"],
    )
    excluded = {
        (row["author_handle"], row["content"])
        for row in favorites + bookmarks
    }
    pending = [
        row for row in tagged
        if (row["author_handle"], row["content"]) not in excluded
    ]
    for row in pending:
        ctx.commit("Favorite record", target=row, values={"favorited": True})
    return len(pending)
"""

    diagnostics = validate_code(source)

    assert any(
        item.code == "COMMIT_TARGET_SOURCE_INACTIVE"
        and "make the target-owning collection current" in item.message
        for item in diagnostics
    )


def test_commit_replay_keeps_target_owning_collection_active() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open saved favorites", success={"entity": "SavedFavorites"})
    favorites_scope = ctx.query(state, entity="SavedFavorites")
    favorites = ctx.acquire(favorites_scope, fields=["author_handle", "content"])
    state = ctx.reach(state, "Open saved bookmarks", success={"entity": "SavedBookmarks"})
    bookmarks_scope = ctx.query(state, entity="SavedBookmarks")
    bookmarks = ctx.acquire(bookmarks_scope, fields=["author_handle", "content"])
    excluded = {
        (row["author_handle"], row["content"])
        for row in favorites + bookmarks
    }
    state = ctx.reach(state, "Open tagged records", success={"entity": "TaggedRecords"})
    tagged_scope = ctx.query(state, entity="TaggedRecords")
    tagged = ctx.acquire(tagged_scope, fields=["id", "author_handle", "content"])
    pending = [
        row for row in tagged
        if (row["author_handle"], row["content"]) not in excluded
    ]
    for row in pending:
        state = ctx.reach(
            state,
            "Open the exact tagged record",
            target=row,
            success={
                "entity": "TaggedRecord",
                "id": row["id"],
                "author_handle": row["author_handle"],
                "content": row["content"],
            },
        )
        state = ctx.commit(state, "Favorite record", target=row, values={"favorited": True})
    return len(pending)
"""
    fixture = FixtureSpec(lookups={
        "savedfavorites": [
            {"id": "saved", "author_handle": "@saved", "content": "old"},
        ],
        "savedbookmarks": [
            {"id": "marked", "author_handle": "@marked", "content": "kept"},
        ],
        "taggedrecords": [
            {"id": "saved", "author_handle": "@saved", "content": "old"},
            {"id": "new", "author_handle": "@new", "content": "fresh"},
        ],
    })

    assert validate_code(source) == []
    result = execute_code(source, fixture)
    assert result.ok, result.error
    assert result.return_value == 1
    assert [write.target_id for write in result.writes] == ["new"]


def test_validate_code_allows_commit_with_global_target_locator() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open source records", success={"entity": "SourceRecords"})
    source_scope = ctx.query(state, entity="SourceRecords")
    rows = ctx.acquire(source_scope, fields=["content", "permalink"])
    state = ctx.reach(state, "Open reference records", success={"entity": "ReferenceRecords"})
    reference_scope = ctx.query(state, entity="ReferenceRecords")
    references = ctx.acquire(reference_scope, fields=["content"])
    reference_content = {row["content"] for row in references}
    pending = [row for row in rows if row["content"] not in reference_content]
    for row in pending:
        state = ctx.reach(
            state,
            "Open the exact source record",
            target=row,
            success={
                "entity": "SourceRecord",
                "content": row["content"],
                "permalink": row["permalink"],
            },
        )
        state = ctx.commit(state, "Update record", target=row, values={"saved": True})
    return len(pending)
"""

    assert validate_code(source) == []


def test_query_owns_fields_but_remains_bound_to_reach_entity() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open records",
        success={"entity": "Records", "fields": ["ID"]},
    )
    scope = ctx.query(state, entity="Records")
    return ctx.acquire(scope, fields=["ID", "Status"])
"""

    fixture = FixtureSpec(lookups={
        "records": [{"ID": "1", "Status": "Complete"}],
    })

    assert execute_code(source, fixture).ok
    mismatched = execute_code(
        source.replace(
            'ctx.query(state, entity="Records")',
            'ctx.query(state, entity="Other")',
        ),
        fixture,
    )
    assert "STATE_ENTITY_MISMATCH" in mismatched.error


def test_validate_code_rejects_literal_state_entity_mismatch() -> None:
    source = """
def run(ctx):
    ctx.reach("Open orders", success={"entity": "Orders"})
    return ctx.query(entity="Shipments", fields=["ID"])
"""

    assert any(
        item.code == "STATE_ENTITY_MISMATCH"
        for item in validate_code(source)
    )


def test_validate_code_requires_current_ui_before_query() -> None:
    source = """
def run(ctx):
    return ctx.query(entity="Orders", fields=["ID"])
"""

    assert any(
        item.code == "ACTIVE_UI_REQUIRED"
        for item in validate_code(source)
    )


def test_validate_code_requires_current_ui_on_every_control_flow_path() -> None:
    source = """
def run(ctx):
    rows = []
    if rows:
        ctx.reach("Open orders", success={"entity": "Orders"})
    return ctx.query(entity="Orders", fields=["ID"])
"""

    assert any(
        item.code == "ACTIVE_UI_REQUIRED"
        for item in validate_code(source)
    )


def test_validate_code_uses_only_the_latest_reach_as_current_ui() -> None:
    source = """
def run(ctx):
    ctx.reach("Open orders", success={"entity": "Orders"})
    ctx.reach("Open shipments", success={"entity": "Shipments"})
    return ctx.query(entity="Orders", fields=["ID"])
"""

    assert any(
        item.code == "STATE_ENTITY_MISMATCH"
        and "active ctx.reach entity 'Shipments'" in item.message
        for item in validate_code(source)
    )


@pytest.mark.parametrize(
    "invalidator",
    [
        'ctx.commit("Save", values={"Status": "Complete"})',
        'ctx.command("back")',
    ],
)
def test_validate_code_requires_new_reach_after_ui_invalidation(
    invalidator: str,
) -> None:
    source = f"""
def run(ctx):
    ctx.reach("Open orders", success={{"entity": "Orders"}})
    {invalidator}
    return ctx.query(entity="Orders", fields=["ID"])
"""

    assert any(
        item.code == "ACTIVE_UI_REQUIRED"
        for item in validate_code(source)
    )


def test_validate_code_rejects_current_ui_use_across_mutating_loop_iterations() -> None:
    source = """
def run(ctx):
    ctx.reach("Open products", success={"entity": "Products"})
    rows = ctx.query(entity="Products", fields=["ID"])
    for row in rows:
        detail = ctx.read(target=row, fields=["Price"])
        ctx.commit("Update product", target=row, values={"Price": detail["Price"]})
"""

    assert any(
        item.code == "ACTIVE_UI_REQUIRED"
        and "may run again" in item.message
        for item in validate_code(source)
    )


def test_validate_code_rejects_legacy_ui_argument() -> None:
    source = """
def run(ctx):
    ctx.reach("Open orders", success={"entity": "Orders"})
    return ctx.query(state, entity="Orders", fields=["ID"])
"""

    assert any(item.code == "CTX_SIGNATURE" for item in validate_code(source))


def test_reach_filters_signature_diagnostic_preserves_route_and_query_boundaries() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open tagged records",
        success={"entity": "TaggedRecords"},
        filters={"tag": "#dogs"},
    )
"""

    diagnostics = validate_code(source)

    assert [item.code for item in diagnostics] == ["CTX_SIGNATURE"]
    assert "following ctx.query(filters=...)" in diagnostics[0].message
    assert "top-level key in reach.success" in diagnostics[0].message


def test_target_reach_restores_route_after_another_source() -> None:
    source = '''
def run(ctx):
    ctx.reach("Tagged", success={"entity": "Tagged", "tag": "#dogs"})
    rows = ctx.query(entity="Tagged", fields=["id"], filters={"tag": "#dogs"})
    ctx.reach("Saved", success={"entity": "Saved"})
    saved = ctx.query(entity="Saved", fields=["id"])
    for row in rows:
        ctx.reach("Open", target=row, success={"entity": "Detail", "id": row["id"]})
        ctx.commit("Change", target=row, values={"enabled": True})
'''

    assert any(
        item.code == "TARGET_REACH_ROUTE_REQUIRED" for item in validate_code(source)
    )


def test_validate_code_requires_direct_read_fields_in_reach_contract() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Show one result", success={"entity": "VisibleResult"})
    return ctx.read(state, fields={"temperature": "number"})
"""

    diagnostics = validate_code(source)

    assert any(
        item.code == "DIRECT_READ_FIELDS_UNDECLARED"
        and "add them to its success.fields list" in item.message
        for item in diagnostics
    )
    assert validate_code(source.replace(
        '{"entity": "VisibleResult"}',
        '{"entity": "VisibleResult", "fields": ["temperature"]}',
    )) == []


def test_validate_code_rejects_reading_observable_reach_dimension() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Configure and render report",
        success={"entity": "Report", "rendered": True},
    )
    result = ctx.read(state, fields={"rendered": "boolean"})
    return result["rendered"]
"""

    diagnostics = validate_code(source)

    assert [item.code for item in diagnostics] == ["DIRECT_READ_SUCCESS_DIMENSION"]
    assert "Remove the redundant read" in diagnostics[0].message
    assert repair_direct_read_fields(source) is None


def test_direct_read_repair_moves_top_level_type_marker_into_fields() -> None:
    source = '''
def run(ctx, state):
    state = ctx.reach(state, "Show weather", success={"entity": "Weather", "temperature": "number"})
    return ctx.read(state, fields={"temperature": "number"})["temperature"]
'''

    assert [item.code for item in validate_code(source)] == [
        "DIRECT_READ_FIELDS_UNDECLARED"
    ]
    repaired = repair_direct_read_fields(source)
    assert repaired is not None
    assert validate_code(repaired) == []
    assert "success={'entity': 'Weather', 'fields': ['temperature']}" in repaired
    assert execute_code(repaired, build_probe_fixture(repaired)).ok


def test_repair_direct_read_fields_strengthens_literal_reach_contract() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Show one result",
        success={"entity": "VisibleResult", "fields": ["title"]},
    )
    first = ctx.read(state, fields={"temperature": "number"})
    second = ctx.read(state, fields=["title", "humidity"])
    return [first, second]
"""

    repaired = repair_direct_read_fields(source)

    assert repaired is not None
    assert validate_code(repaired) == []
    assert "'fields': ['title', 'temperature', 'humidity']" in repaired


def test_validate_code_tracks_reassigned_state_by_call_order() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open customers", success={"entity": "Customers"})
    customers_scope = ctx.query(state, entity="Customers")
    customers = ctx.acquire(customers_scope, fields=["Name"])
    state = ctx.reach(state, "Open orders", success={"entity": "Orders"})
    orders_scope = ctx.query(state, entity="Orders")
    orders = ctx.acquire(orders_scope, fields=["ID"])
    return [customers, orders]
"""

    assert validate_code(source) == []


def test_validate_code_accepts_assigning_and_returning_reach_result() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open form", success={"entity": "Form"})
    return state
"""

    assert validate_code(source) == []


def test_coding_runtime_normalizes_executor_terminal_phase() -> None:
    outcome = CodingProgramRuntime.adapt_outcome(StatementOutcome.exhausted(
        "current call cannot establish its postcondition",
        evidence=["frame:1"],
    ))

    assert outcome.phase == "failed"
    assert outcome.evidence == ["frame:1"]


def test_validate_code_does_not_require_an_unnecessary_assertion() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Status"],
        },
    )
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope, fields=["Status"])
    return [row["Status"] for row in rows]
"""

    assert validate_code(source) == []


def test_validate_code_allows_exact_query_field_spelling_shared_with_filter() -> None:
    source = '''
def run(ctx, state):
    state = ctx.reach(state, "Open pages", success={"entity": "Pages"})
    scope = ctx.query(
        state,
        entity="Pages",
        filters={"Title": "Home Page"},
    )
    return ctx.acquire(scope, fields=["Title"])
'''

    assert validate_code(source) == []


def test_runtime_dataflow_treats_reach_as_an_effect() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open the editor", success={"entity": "Record"})
    state = ctx.commit(state, "Create a record", values={"Name": "Example"})
"""

    assert validate_runtime_dataflow(source) == []


def test_validate_code_rejects_reach_before_direct_creation_commit() -> None:
    source = """
def run(ctx):
    name = "Example"
    ctx.reach("Open form", success={"entity": "Records"})
    ctx.commit("Create record", target=None, values={"Name": name})
"""

    assert any(
        item.code == "DIRECT_COMMIT_REQUIRED"
        for item in validate_code(source)
    )


def test_validate_code_requires_target_when_reach_identifies_existing_record() -> None:
    source = '''
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open existing product",
        success={"entity": "Products", "Name": "Selene Yoga Hoodie"},
    )
    state = ctx.commit(state, "Update product", values={"Short Description": "New"})
'''

    diagnostics = validate_code(source)

    assert [item.code for item in diagnostics] == ["COMMIT_TARGET_REQUIRED"]
    assert "Query and project that record identity" in diagnostics[0].message


def test_validate_code_rejects_empty_target_commit_values() -> None:
    source = """
def run(ctx):
    target = {"ID": "1"}
    ctx.reach("Open record", target=target, success={"entity": "Record", "ID": target["ID"]})
    ctx.commit("Update record", target=target, values={})
"""

    assert any(
        item.code == "TARGET_COMMIT_VALUES_REQUIRED"
        for item in validate_code(source)
    )


def test_validate_code_allows_source_read_before_schema_free_commit() -> None:
    source = '''
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open source text",
        success={"entity": "Message", "fields": ["body"]},
    )
    detail = ctx.read(state, fields={"body": "text"})
    state = ctx.commit(state, f"Create from source: {detail['body']}", values={})
'''

    assert validate_code(source) == []


def test_schema_free_commit_cannot_replace_source_with_host_transformation() -> None:
    source = '''
def run(ctx, state):
    state = ctx.reach(state, "Open source", success={"entity": "Message", "fields": ["body"]})
    detail = ctx.read(state, fields={"body": "text"})
    body = detail["body"]
    transformed = str(body)
    state = ctx.commit(state, f"Create from {transformed}", values={})
'''

    assert any(
        item.code == "SCHEMA_FREE_SOURCE_REQUIRED" for item in validate_code(source)
    )


def test_runtime_completes_terminal_reach_without_exposing_ui_state(request) -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Configure and show the Orders report",
        success={
            "entity": "Sales Reports",
            "Report Subtype": "Orders",
            "From": "05/01/2021",
            "To": "03/31/2022",
            "rendered": True,
        },
    )
"""
    runtime = CodingProgramRuntime.start(CodingProgram(
        goal="Show the requested Orders report",
        source=source,
    ))
    request.addfinalizer(runtime.close)

    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current.statement.expected_state["From"] == "05/01/2021"
    assert runtime.current.statement.expected_state["rendered"] is True
    runtime.send_outcome(StatementOutcome.completed("report rendered"))

    assert runtime.reply == "Coding program completed"


def test_projection_contract_tracks_query_rows() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open orders", success={"entity": "Orders"})
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope, fields=["Status"])
    assert rows, "orders are required"
    return [row["Total"] for row in rows]
"""

    diagnostics = validate_projection_contract(source)

    assert any(item.code == "PROJECTED_FIELD_UNAVAILABLE" for item in diagnostics)


def test_projection_contract_tracks_rank_key_lambda_fields() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open orders", success={"entity": "Orders"})
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope,
        fields={"Grand Total": "money"},
    )
    return sorted(rows, key=lambda row: row["Purchase Date"])
"""

    assert any(
        item.code == "PROJECTED_FIELD_UNAVAILABLE"
        for item in validate_projection_contract(source)
    )


def test_runtime_dataflow_requires_consuming_read_values() -> None:
    source = """
def run(ctx):
    row = ctx.query(entity="Products", fields=["id"])[0]
    state = ctx.read(target=row, fields=["Price"])
    assert row, "product is required"
    return row
"""

    diagnostics = validate_runtime_dataflow(source)

    assert any(item.code == "UNUSED_RUNTIME_VALUE" for item in diagnostics)


def test_fixture_contract_checks_query_fields_for_matching_source() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open orders", success={"entity": "Orders"})
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope, fields=["Missing"])
    assert rows, "orders are required"
    return rows
"""

    diagnostics = validate_fixture_contract(
        source,
        FixtureSpec(lookups={"orders": [{"ID": "1", "Status": "Complete"}]}),
        match_lookup_sources=True,
    )

    assert any(item.code == "MOCK_FIELD_UNAVAILABLE" for item in diagnostics)


def test_fixture_contract_requires_reach_context_for_absent_query_source() -> None:
    source = """
def run(ctx, state):
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope, fields=["ID"])
    assert rows, "orders are required"
    return rows
"""

    diagnostics = validate_fixture_contract(
        source,
        FixtureSpec(lookups={"products": [{"ID": "1"}]}),
        match_lookup_sources=True,
    )

    assert any(item.code == "QUERY_CONTEXT_REQUIRED" for item in diagnostics)


def test_fixture_contract_keeps_collection_and_detail_sources_separate() -> None:
    source = """
def run(ctx):
    ctx.reach("Open reviews", success={
        "entity": "Reviews", "fields": ["Title"],
    })
    detail = ctx.read(target={"Title": "review"}, fields=["Nickname"])
    return detail["Nickname"]
"""
    dashboard = FixtureSpec(lookups={"dashboard": [{"Nickname": "summary"}]})
    explicit_detail = FixtureSpec(reads={"review": {"Rating": 3}})

    assert validate_fixture_contract(source, dashboard) == []
    assert any(
        item.code == "MOCK_FIELD_UNAVAILABLE"
        for item in validate_fixture_contract(source, explicit_detail)
    )


def test_execute_code_filters_normalizes_and_returns_query_rows() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": [
                "Status",
                "Purchase Date",
                "Grand Total (Purchased)",
            ],
        },
    )
    scope = ctx.query(state, entity="Orders",
        filters={"Status": "Complete"},
    )
    rows = ctx.acquire(scope,
        fields={
            "Purchase Date": "datetime",
            "Grand Total (Purchased)": "number",
        },
    )
    assert len(rows) == 2, "two complete orders are required"
    return rows
"""
    fixture = FixtureSpec(lookups={"orders": [
        {
            "Status": "Complete",
            "Purchase Date": "Jun 9, 2023 9:00:00 AM",
            "Grand Total (Purchased)": "$100.00",
        },
        {
            "Status": "Pending",
            "Purchase Date": "Jun 10, 2023 9:00:00 AM",
            "Grand Total (Purchased)": "$900.00",
        },
        {
            "Status": "Complete",
            "Purchase Date": "May 31, 2023 9:00:00 AM",
            "Grand Total (Purchased)": "$82.40",
        },
    ]})

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert [row["Grand Total (Purchased)"] for row in result.return_value] == [100.0, 82.4]
    assert all(
        row["Purchase Date"].year == 2023
        for row in result.return_value
    )
    assert result.trace[0].op == "reach"
    assert result.trace[1].op == "query"
    assert result.trace[1].kwargs["filters"] == {"Status": "Complete"}


def test_probe_fixture_supports_typed_fields_and_structured_range_filters() -> None:
    source = """
def run(ctx, state):
    start_date = "01/01/2023"
    end_date = "05/31/2023"
    state = ctx.reach(state, "Open orders", success={"entity": "Orders"})
    scope = ctx.query(state, entity="Orders",
        filters={
            "Status": "Complete",
            "Purchase Date": {
                "from": start_date,
                "to": end_date,
            },
        },
    )
    rows = ctx.acquire(scope, fields={"Purchase Date": "datetime"})
    return rows[0]["Purchase Date"].year
"""

    result = execute_code(source, build_probe_fixture(source))

    assert result.ok, result.error
    assert result.return_value == 2023


def test_fixture_query_does_not_fuzz_or_correct_literal_filter_phrase() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open products", success={"entity": "Products"})
    scope = ctx.query(state, entity="Products",
        filters={"Name": "Auroar"},
    )
    return ctx.acquire(scope, fields=["Name"])
"""
    fixture = FixtureSpec(lookups={
        "products": [{"Name": "Aurora jacket"}],
    })

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.return_value == []


def test_execute_code_commit_updates_fixture_state() -> None:
    result = execute_code(GOOD_PROGRAM, _fixture())

    assert result.ok, result.error
    assert result.return_value == 1
    assert result.final_state["p1"]["Price"] == 80.0
    assert result.writes[0].target_id == "p1"
    assert result.writes[0].required_values == {"Price": 80.0}
    assert [event.op for event in result.trace] == [
        "reach",
        "query",
        "query",
        "read",
        "reach",
        "commit",
    ]


def test_fixture_query_normalizes_unique_phone_alias() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open the customer collection",
        success={
            "entity": "+1 205 881 2302",
            "fields": ["id", "phone"],
        },
    )
    scope = ctx.query(state, entity="+1 205 881 2302")
    rows = ctx.acquire(scope, fields=["id", "phone"])
    assert len(rows) == 1, "one customer is required"
    return rows[0]["id"]
"""
    fixture = FixtureSpec(lookups={
        "2058812302": [{"id": "c1", "phone": "2058812302"}],
    })

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.return_value == "c1"


def test_fixture_semantic_fields_ignore_case_spaces_and_underscores() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Grand Total (Purchased)"],
        },
    )
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope, fields=["Grand Total (Purchased)"])
    assert rows, "orders are required"
    return rows[0]["Grand Total (Purchased)"]
"""
    fixture = FixtureSpec(lookups={
        "orders": [{"grand_total_purchased": "$12.50"}],
    })

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.return_value == 12.5


def test_fixture_preserves_reach_commit_evidence_when_later_code_fails() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open products",
        success={
            "entity": "Products",
            "fields": ["id"],
        },
    )
    scope = ctx.query(state, entity="Products")
    product = ctx.acquire(scope, fields=["id"])[0]
    state = ctx.reach(
        state,
        "Open the exact product",
        target=product,
        success={"entity": "Product", "id": product["id"]},
    )
    state = ctx.commit(state, "Update price", target=product, values={"Price": 80})
    assert product["missing"], "later check fails"
"""
    fixture = FixtureSpec(
        lookups={"products": [{"id": "p1", "Price": 100}]},
        reads={"p1": {"Price": 100}},
    )

    result = execute_code(source, fixture)

    assert not result.ok
    assert result.writes[0].target_id == "p1"
    assert result.final_state["p1"]["Price"] == 80


def test_probe_fixture_supports_query_filters_and_numeric_fields() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Status", "Grand Total (Purchased)"],
        },
    )
    scope = ctx.query(state, entity="Orders",
        filters={"Status": "Complete"},
    )
    rows = ctx.acquire(scope, fields=["Status", "Grand Total (Purchased)"])
    assert rows, "orders are required"
    return sum(row["Grand Total (Purchased)"] for row in rows)
"""

    fixture = build_probe_fixture(source)
    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.return_value > 0


def test_probe_read_target_survives_query_field_merge_and_dynamic_filter() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open reviews", success={"entity": "Reviews"})
    product_filter = {"Product": "Olivia jacket"}
    scope = ctx.query(state, entity="Reviews", filters=product_filter)
    products = ctx.acquire(scope, fields=["Product"])
    product = products[0]["Product"]
    scope2 = ctx.query(state, entity="Reviews", filters={"Product": product})
    rows = ctx.acquire(scope2, fields=["Nickname", "Product"])
    return [
        row["Nickname"]
        for row in rows
        if ctx.read(state, target=row, fields=["Rating"])["Rating"] <= 3
    ]
"""

    result = execute_code(source, build_probe_fixture(source))

    assert result.ok, result.error
    assert len(result.return_value) == 3


def test_probe_target_read_survives_typed_query_normalization() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Show results", success={"entity": "Results"})
    scope = ctx.query(state, entity="Results")
    rows = ctx.acquire(scope, fields={"temperature": "number"})
    return ctx.read(state, target=rows[0], fields={"temperature": "number"})["temperature"]
"""

    result = execute_code(source, build_probe_fixture(source))

    assert result.ok, result.error
    assert isinstance(result.return_value, (int, float))


def test_probe_fixture_supports_read_directly_from_reached_state() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Render connectivity settings",
        success={"entity": "DeviceSettings", "fields": ["flight_mode"]},
    )
    current = ctx.read(state, fields=["flight_mode"])
    return current["flight_mode"]
"""

    fixture = build_probe_fixture(source)
    result = execute_code(source, fixture)

    assert fixture.reads["None"]["flight_mode"]
    assert result.ok, result.error
    assert [event.op for event in result.trace] == ["reach", "read"]


def test_probe_fixture_accepts_dynamic_filter_field_not_in_projection() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open products", success={"entity": "Products"})
    scope = ctx.query(state, entity="Products",
        filters={"Name": "Example"},
    )
    products = ctx.acquire(scope, fields=["Name"])
    state = ctx.reach(state, "Open reviews", success={"entity": "All Reviews"})
    scope2 = ctx.query(state, entity="All Reviews",
        filters={"Product": products[0]["Name"]},
    )
    return ctx.acquire(scope2, fields=["Action"])
"""

    result = execute_code(source, build_probe_fixture(source))

    assert result.ok, result.error


def test_runtime_query_yields_lookup_then_constrain_then_acquire() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": [
                "Status",
                "Purchase Date",
                "Grand Total (Purchased)",
            ],
        },
    )
    scope = ctx.query(state, entity="Orders",
        filters={"Status": "Complete"},
    )
    rows = ctx.acquire(scope,
        fields={
            "Purchase Date": "datetime",
            "Grand Total (Purchased)": "number",
        },
    )
    assert rows, "orders are required"
    return rows[0]["Grand Total (Purchased)"]
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="sum orders", source=source))

    # Public ctx.reach establishes the one active GUI state consumed by ctx.query.
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current.statement.interaction_intent is None
    assert runtime.current.statement.expected_state == {
        "entity": "Orders",
        "fields": [
            "Status",
            "Purchase Date",
            "Grand Total (Purchased)",
        ],
    }
    runtime.send_outcome(StatementOutcome.completed("orders available"))

    # 1. lookup — pure locate, filters no longer ride on the lookup request.
    assert isinstance(runtime.current.statement, Interact)
    lookup_intent = runtime.current.statement.interaction_intent
    assert isinstance(lookup_intent, CollectionIntent)
    assert lookup_intent.phase == "locate"
    ui_state_token = runtime.current.args["ui_state_token"]
    assert ui_state_token.endswith(":state")
    assert runtime.current.inputs["ui_state"]["token"] == ui_state_token
    assert (
        runtime.current.inputs["ui_state"]["postcondition"]["entity"]
        == "Orders"
    )
    assert (
        runtime.interpreter.run_log[0].coding_payload["produced_state"]
        == ui_state_token
    )
    assert lookup_intent.required_fields == ["status"]
    assert "number of records" in runtime.current.statement.goal
    assert "row count is unrestricted" in runtime.current.statement.success
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 2
    query_call_id = runtime.current_coding_call_id
    assert query_call_id
    assert query_call_id != runtime.interpreter.run_log[0].coding_call_id
    runtime.send_outcome(StatementOutcome.completed(
        "scope resolved",
        outputs={"scope": {
            "kind": "resolved_collection",
            "entity": "Orders",
            "surface_fingerprint": "table:#orders",
            "available_fields": ["Purchase Date", "Grand Total (Purchased)"],
        }},
    ))

    # 2. constrain — filters have a structured home here, not in the lookup goal.
    assert isinstance(runtime.current.statement, Interact)
    constrain_intent = runtime.current.statement.interaction_intent
    assert isinstance(constrain_intent, CollectionIntent)
    assert constrain_intent.phase == "constrain"
    assert constrain_intent.required_fields == [
        "Purchase Date",
        "Grand Total (Purchased)",
    ]
    assert runtime.current.args["ui_state_token"] == ui_state_token
    assert runtime.interpreter.run_log[-1].coding_payload["state"] == ui_state_token
    assert constrain_intent.predicates["status"].values == ["complete"]
    assert runtime.current_coding_plan_step == 2
    assert runtime.current_coding_plan_steps == 2
    assert runtime.current_coding_call_id == query_call_id
    filtered_scope = {
        "kind": "resolved_collection",
        "entity": "Orders",
        "surface_fingerprint": "table:#filtered-orders",
        "available_fields": ["Purchase Date", "Grand Total (Purchased)"],
    }
    runtime.send_outcome(StatementOutcome.completed(
        "filter active",
        outputs={"scope": filtered_scope},
    ))

    # 3. acquire — materialize the now-constrained collection as its own call.
    assert isinstance(runtime.current.statement, Acquire)
    assert runtime.current.args["ui_state_token"] == (
        f"scope:{filtered_scope['surface_fingerprint']}"
    )
    assert runtime.current.args["lookup_scope"] == filtered_scope
    assert runtime.current.args["field_types"] == {
        "Purchase Date": "datetime",
        "Grand Total (Purchased)": "number",
    }
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 1
    assert runtime.current_coding_call_id != query_call_id
    runtime.send_outcome(StatementOutcome.completed(
        "rows acquired",
        outputs={"rows": [{
            "Purchase Date": "Jun 9, 2023 9:00:00 AM",
            "Grand Total (Purchased)": "$100.00",
        }]},
    ))

    assert runtime.finished
    assert runtime.reply == "100"
    assert [record.coding_op for record in runtime.interpreter.run_log] == [
        "reach",
        "lookup",
        "constrain",
        "acquire",
    ]


def test_runtime_query_without_predicates_skips_constrain() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open terms", success={"entity": "Terms"})
    scope = ctx.query(state, entity="Terms")
    return ctx.acquire(scope, fields=["Term", "Uses"])
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="list terms", source=source))

    runtime.send_outcome(StatementOutcome.completed("terms available"))
    assert runtime.current.statement.interaction_intent.phase == "locate"
    query_call_id = runtime.current_coding_call_id
    assert runtime.current_coding_plan_steps == 1
    runtime.send_outcome(StatementOutcome.completed(
        "scope resolved",
        outputs={"scope": {
            "kind": "resolved_collection",
            "entity": "Terms",
            "surface_fingerprint": "table:#terms",
            "available_fields": ["Term", "Uses"],
        }},
    ))

    assert isinstance(runtime.current.statement, Acquire)
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 1
    assert runtime.current_coding_call_id != query_call_id
    runtime.send_outcome(StatementOutcome.completed(
        "rows acquired",
        outputs={"rows": [{"Term": "bag", "Uses": 10}]},
    ))

    assert runtime.finished
    assert [record.coding_op for record in runtime.interpreter.run_log] == [
        "reach",
        "lookup",
        "acquire",
    ]


def test_runtime_query_reuses_matching_route_filter_from_current_ui() -> None:
    source = '''
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open #dogs",
        success={"entity": "TaggedToots", "tag": "#dogs"},
    )
    scope = ctx.query(
        state,
        entity="TaggedToots",
        filters={"tag": "#dogs"},
    )
    return ctx.acquire(scope, fields=["author_handle", "content"])
'''
    runtime = CodingProgramRuntime.start(CodingProgram(goal="list tagged posts", source=source))

    runtime.send_outcome(StatementOutcome.completed("tag route active"))
    assert runtime.current.statement.interaction_intent.phase == "locate"
    # The reach already established the tag route, so the lookup has no extra
    # required fields and no constrain phase is needed.
    assert runtime.current.statement.interaction_intent.required_fields == []
    assert runtime.current_coding_plan_steps == 1
    runtime.send_outcome(StatementOutcome.completed(
        "scope resolved",
        outputs={"scope": {
            "kind": "resolved_collection",
            "entity": "TaggedToots",
            "surface_fingerprint": "android-collection:feed",
            "available_fields": [],
            "projection": "cells",
        }},
    ))

    assert isinstance(runtime.current.statement, Acquire)
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 1
    runtime.send_outcome(StatementOutcome.completed(
        "rows acquired",
        outputs={"rows": []},
    ))
    assert [record.coding_op for record in runtime.interpreter.run_log] == [
        "reach",
        "lookup",
        "acquire",
    ]


def test_runtime_program_explicitly_branches_from_full_to_short_phrase() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open products", success={"entity": "Products"})
    scope = ctx.query(state, entity="Products",
        filters={"Name": "Aurora jacket"},
    )
    rows = ctx.acquire(scope, fields=["Name"])
    if not rows:
        scope = ctx.query(state, entity="Products",
            filters={"Name": "Aurora"},
        )
        rows = ctx.acquire(scope, fields=["Name"])
    return rows
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="find product", source=source))
    scope = {
        "kind": "resolved_collection",
        "entity": "Products",
        "surface_fingerprint": "table:#products",
        "available_fields": ["Name"],
    }

    runtime.send_outcome(StatementOutcome.completed("products available"))
    runtime.send_outcome(StatementOutcome.completed(
        "scope resolved", outputs={"scope": scope},
    ))
    runtime.send_outcome(StatementOutcome.completed(
        "exact filter active", outputs={"scope": scope},
    ))
    runtime.send_outcome(StatementOutcome.completed(
        "no exact rows", outputs={"rows": []},
    ))

    assert runtime.current.statement.interaction_intent.phase == "locate"
    runtime.send_outcome(StatementOutcome.completed(
        "short-phrase scope resolved", outputs={"scope": scope},
    ))
    assert runtime.current.statement.interaction_intent.phase == "constrain"
    assert (
        runtime.current.statement.interaction_intent.predicates["name"].values
        == ["aurora"]
    )

    runtime.send_outcome(StatementOutcome.completed(
        "short-phrase filter active", outputs={"scope": scope},
    ))
    runtime.send_outcome(StatementOutcome.completed(
        "short-phrase rows acquired",
        outputs={"rows": [{"Name": "Aurora jacket waterproof"}]},
    ))

    assert runtime.finished
    assert "Aurora jacket waterproof" in runtime.reply


def test_runtime_commit_infers_internal_statement_contract() -> None:
    source = """
def run(ctx, state):
    target = {"ID": "1"}
    state = ctx.reach(
        state,
        "Open the exact order",
        target=target,
        success={"entity": "Order", "ID": target["ID"]},
    )
    state = ctx.commit(state, "Update order status", target=target, values={"Status": "Complete"})
    assert ctx, "runtime exists"
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="update order", source=source))

    runtime.send_outcome(StatementOutcome.completed("exact order visible"))
    statement = runtime.current.statement
    assert isinstance(statement, Interact)
    assert statement.goal == "Update order status"
    assert statement.required_values == {"Status": "Complete"}
    assert statement.persistence == "explicit_commit"
    assert runtime.current.inputs["target"] == {"ID": "1"}
    assert runtime.current.inputs["ui_state"]["target"] == {"ID": "1"}


def test_source_derived_semantic_commit_keeps_an_explicit_commit_boundary() -> None:
    source = '''
def run(ctx, state):
    source_text = "Lunch tomorrow at 11 AM for one hour"
    state = ctx.commit(state, "Create an entry from this source: " + source_text, values={})
'''

    assert not validate_code(source)
    probe = execute_code(source, FixtureSpec())
    assert probe.ok, probe.error
    assert probe.trace[-1].op == "commit"
    assert probe.writes[-1].required_values == {}

    runtime = CodingProgramRuntime.start(CodingProgram(goal="create entry", source=source))
    statement = runtime.current.statement
    assert isinstance(statement, Interact)
    assert statement.required_values == {}
    assert statement.persistence == "explicit_commit"


def test_commit_date_time_values_match_probe_and_runtime_json_contract() -> None:
    source = """
from datetime import date, datetime, time

def run(ctx, state):
    state = ctx.commit(
        state,
        "Set a weekend alarm",
        values={
            "time": time(8, 25),
            "start_date": date(2026, 7, 28),
            "created_at": datetime(2026, 7, 28, 8, 25),
            "days": ["Saturday", "Sunday"],
        },
    )
"""
    expected = {
        "time": "08:25:00",
        "start_date": "2026-07-28",
        "created_at": "2026-07-28T08:25:00",
        "days": ["Saturday", "Sunday"],
    }

    probe = execute_code(source, build_probe_fixture(source))
    assert probe.ok, probe.error
    assert probe.writes[0].required_values == expected

    runtime = CodingProgramRuntime.start(
        CodingProgram(goal="set alarm", source=source)
    )
    try:
        assert isinstance(runtime.current.statement, Interact)
        assert runtime.current.statement.required_values == expected
        assert not runtime.interpreter.control_error
    finally:
        runtime.close()


def test_runtime_read_target_remains_one_public_call_with_internal_focus() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["ID"],
        },
    )
    detail = ctx.read(state, target={"ID": "1"}, fields=["Status"])
    assert detail["Status"], "status is required"
    return detail["Status"]
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="read order", source=source))

    assert runtime.current.statement.interaction_intent is None
    assert runtime.current.statement.expected_state == {
        "entity": "Orders",
        "fields": ["ID"],
    }
    runtime.send_outcome(StatementOutcome.completed("orders available"))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current.inputs["target"] == {"ID": "1"}
    parent_token = runtime.current.args["ui_state_token"]
    assert runtime.current.inputs["ui_state"]["token"] == parent_token
    runtime.send_outcome(StatementOutcome.completed("target focused"))
    read_call_id = runtime.interpreter.run_log[-1].coding_call_id
    assert isinstance(runtime.current.statement, Read)
    assert runtime.current.args["ui_state_token"] != parent_token
    child_token = runtime.current.args["ui_state_token"]
    assert (
        runtime.interpreter.run_log[-1].coding_payload["produced_state"]
        == child_token
    )
    assert runtime.current_coding_payload["state"] == child_token
    assert runtime.current_coding_call_id == read_call_id
    assert (
        runtime.current.inputs["ui_state"]["postcondition"]["kind"]
        == "target_fields_available"
    )
    assert runtime.current.inputs["ui_state"]["postcondition"]["target"] == {
        "ID": "1"
    }
    runtime.send_outcome(StatementOutcome.completed(
        "status read",
        outputs={"Status": "Complete"},
    ))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current_coding_op == "restore_source"
    assert runtime.current.statement.expected_state == {
        "entity": "Orders",
        "fields": ["ID"],
    }

    runtime.send_outcome(StatementOutcome.completed("orders restored"))
    assert runtime.reply == "Complete"


def test_runtime_read_uses_unique_row_url_as_deterministic_transport() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open reviews", success={"entity": "Reviews"})
    return ctx.read(state, target={"ID": "351", "Action_url": "https://example.test/reviews/351"},
        fields={"Rating": "number"},
    )
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="read review", source=source))

    runtime.send_outcome(StatementOutcome.completed("reviews available"))
    assert isinstance(runtime.current.statement, Command)
    assert runtime.current.statement.capability == "open_url"
    assert runtime.current.args["url"] == "https://example.test/reviews/351"
    assert runtime.current_coding_op == "open_target"
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 4

    runtime.send_outcome(StatementOutcome.completed("detail opened"))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current.statement.observe_fields == ["Rating"]
    assert runtime.current_coding_op == "focus"
    assert runtime.current_coding_plan_step == 2
    assert runtime.current.inputs["ui_state"]["postcondition"] == {
        "kind": "target_open",
        "target": {
            "ID": "351",
            "Action_url": "https://example.test/reviews/351",
        },
    }

    runtime.send_outcome(StatementOutcome.completed("rating exposed"))
    assert isinstance(runtime.current.statement, Read)
    assert runtime.current_coding_plan_step == 3
    assert runtime.current.args["field_types"] == {"Rating": "number"}
    runtime.send_outcome(StatementOutcome.completed(
        "rating read",
        outputs={"Rating": "3 stars"},
    ))
    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current_coding_op == "restore_source"
    assert runtime.current_coding_plan_step == 4
    assert runtime.current.statement.expected_state == {
        "entity": "Reviews",
    }

    runtime.send_outcome(StatementOutcome.completed("reviews restored"))
    assert runtime.reply == '{"Rating": 3}'


def test_generate_code_accepts_validated_program() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
    )

    plan = generate_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.source.strip() == GOOD_PROGRAM.strip()
    assert [event.kind for event in plan.events] == [
        "generation_started",
        "generation_completed",
        "diagnostics",
        "probe",
        "finalized",
    ]


def test_synthetic_cardinality_assertion_does_not_trigger_regeneration() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open pages", success={"entity": "Pages"})
    scope = ctx.query(state, entity="Pages",
        filters={"Title": "Home Page"},
    )
    rows = ctx.acquire(scope, fields=["Title"])
    assert len(rows) == 1, "one page is required"
    state = ctx.reach(
        state,
        "Open the exact page",
        target=rows[0],
        success={"entity": "Page", "Title": rows[0]["Title"]},
    )
    state = ctx.commit(state, "Update page title", target=rows[0], values={"Page Title": "New title"})
"""
    llm = _SequenceLLM(
        f"```python\n{source}\n```",
    )

    plan = generate_code(
        "update one page",
        knowledge="Pages exposes Title and supports an existing targeted Page mutation.",
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert len(plan.attempts) == 1
    assert plan.attempts[0].run is not None and plan.attempts[0].run.ok


def test_synthetic_business_value_error_does_not_trigger_regeneration() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open products", success={"entity": "Products"})
    scope = ctx.query(state, entity="Products",
        filters={"Name": "Example"},
    )
    rows = ctx.acquire(scope, fields=["Name", "Type"])
    owners = [row for row in rows if row["Type"] == "Configurable Product"]
    if len(owners) != 1:
        raise ValueError("one configurable owner is required")
    state = ctx.reach(
        state,
        "Open the exact product",
        target=owners[0],
        success={
            "entity": "Product",
            "Name": owners[0]["Name"],
            "Type": owners[0]["Type"],
        },
    )
    state = ctx.commit(state, "Update owner", target=owners[0], values={"Status": "Enabled"})
"""
    llm = _SequenceLLM(
        f"```python\n{source}\n```",
    )

    plan = generate_code(
        "update one configurable product",
        knowledge="Products exposes Name and Type for an existing targeted Product mutation.",
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert len(plan.attempts) == 1


def test_generate_code_regenerates_whole_program() -> None:
    bad = GOOD_PROGRAM.replace('fields=["Price"]', 'fields=["Missing"]')
    llm = _SequenceLLM(
        f"```python\n{bad}\n```",
        f"```python\n{GOOD_PROGRAM}\n```",
    )

    plan = generate_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.source.strip() == GOOD_PROGRAM.strip()
    assert plan.repaired
    assert len(plan.attempts) == 2
    regeneration_prompt = "\n".join(
        str(message.content) for message in llm.messages[1]
    )
    assert "complete replacement program" in regeneration_prompt


def test_generate_code_deterministically_repairs_direct_read_fields() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Search for the requested weather",
        success={"entity": "SearchResults"},
    )
    result = ctx.read(state, fields={"temperature": "number"})
    return int(result["temperature"])
"""
    llm = _SequenceLLM(f"```python\n{source}\n```")

    plan = generate_code("return the visible temperature", llm=llm)

    assert plan.requirements_satisfied
    assert plan.repaired
    assert len(llm.messages) == 1
    assert "'fields': ['temperature']" in plan.source
    assert any(
        event.kind == "deterministic_repair_completed"
        for event in plan.events
    )


def test_generate_code_repairs_direct_read_fields_after_regeneration() -> None:
    invalid = """
def run(ctx, state):
    state = ctx.reach(state, "Open results", success={"entity": "Results"})
    scope = ctx.query(state, entity="Other")
    return ctx.acquire(scope, fields=["value"])
"""
    repairable = """
def run(ctx, state):
    state = ctx.reach(state, "Show the result", success={"entity": "VisibleResult"})
    result = ctx.read(state, fields={"value": "number"})
    return result["value"]
"""
    llm = _SequenceLLM(
        f"```python\n{invalid}\n```",
        f"```python\n{repairable}\n```",
    )

    plan = generate_code("return the visible value", llm=llm)

    assert plan.requirements_satisfied
    assert len(llm.messages) == 2
    assert len(plan.attempts) == 3
    assert "'fields': ['value']" in plan.source
    assert plan.events[-1].data["repair_status"] == "deterministic"


def test_static_diagnostics_trigger_one_regeneration() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["ID"],
        },
    )
    scope = ctx.query(state, entity="Orders")
    rows = ctx.acquire(scope)
    return len(rows)
"""
    repaired = source.replace(
        '    rows = ctx.acquire(scope)',
        '    rows = ctx.acquire(scope, fields=["ID"])',
    )
    llm = _SequenceLLM(
        f"```python\n{source}\n```",
        f"```python\n{repaired}\n```",
    )

    plan = generate_code(
        "count orders",
        fixture=FixtureSpec(lookups={"orders": [{"ID": "1"}]}),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.source.strip() == repaired.strip()
    assert len(llm.messages) == 2


def test_regeneration_still_requires_a_valid_program() -> None:
    invalid = GOOD_PROGRAM.replace(
        'fields=["Price"]',
        'fields=["Missing"]',
    )
    llm = _SequenceLLM(
        f"```python\n{invalid}\n```",
        "def run(ctx):\n    return missing",
        "def run(ctx):\n    return missing",
        "def run(ctx):\n    return missing",
    )

    plan = generate_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert not plan.requirements_satisfied
    assert len(llm.messages) == 4
    with pytest.raises(CodingCompileError):
        program_from_plan(plan)


def test_generate_code_retries_when_first_replacement_is_still_invalid() -> None:
    unsafe_replacement = """
import re

def run(ctx):
    return re.search("value", "value").group(0)
"""
    valid = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Show the visible result",
        success={"entity": "VisibleResult", "fields": ["value"]},
    )
    result = ctx.read(state, fields={"value": "number"})
    return int(result["value"])
"""
    wrong_read_shape = valid.replace('result["value"]', "result")
    llm = _SequenceLLM(
        f"```python\n{wrong_read_shape}\n```",
        f"```python\n{unsafe_replacement}\n```",
        f"```python\n{valid}\n```",
    )

    plan = generate_code("return one visible numeric value", llm=llm)

    assert plan.requirements_satisfied
    assert plan.source.strip() == valid.strip()
    assert len(llm.messages) == 3
    assert [
        event.data["phase"]
        for event in plan.events
        if event.kind == "generation_started"
    ] == ["initial", "regenerated", "regenerated_2"]


def test_regeneration_prompt_retains_prior_diagnostics() -> None:
    missing_reach = '''
def run(ctx, state):
    scope = ctx.query(state, entity="Orders")
    return ctx.acquire(scope, fields=["ID"])
'''
    wrong_state = '''
def run(ctx, state):
    state = ctx.reach(state, "Other", success={"entity": "Other"})
    scope = ctx.query(state, entity="Orders")
    return ctx.acquire(scope, fields=["ID"])
'''
    valid = '''
def run(ctx, state):
    state = ctx.reach(state, "Orders", success={"entity": "Orders"})
    scope = ctx.query(state, entity="Orders")
    return ctx.acquire(scope, fields=["ID"])
'''
    llm = _SequenceLLM(
        f"```python\n{missing_reach}\n```",
        f"```python\n{wrong_state}\n```",
        f"```python\n{valid}\n```",
    )

    plan = generate_code(
        "list orders",
        fixture=FixtureSpec(lookups={"orders": [{"ID": "1"}]}),
        llm=llm,
    )

    assert plan.requirements_satisfied
    second_regeneration = "\n".join(
        str(message.content) for message in llm.messages[2]
    )
    assert "ACTIVE_UI_REQUIRED" in second_regeneration
    assert "STATE_ENTITY_MISMATCH" in second_regeneration


def test_generator_receives_knowledge_and_api_schema() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
    )

    generate_code(
        "discount Sahara leggings",
        knowledge="Product Price is available on the product detail page.",
        fixture=_fixture(),
        llm=llm,
    )

    generation_text = "\n".join(
        str(message.content)
        for message in llm.messages[0]
    )
    assert "Product Price is available" in generation_text
    assert "collection sources" in generation_text


def test_unstructured_visual_contract_rejects_invented_collection() -> None:
    source = '''
def run(ctx):
    ctx.reach("Show result", success={"entity": "Result"})
    rows = ctx.query(entity="Result", fields={"value": "number"})
    return int(rows[0]["value"])
'''

    diagnostics = _unstructured_visual_diagnostics(source)

    assert [item.code for item in diagnostics] == ["UNSTRUCTURED_QUERY_FORBIDDEN"]
    assert "one typed direct ctx.read" in diagnostics[0].message


def test_planner_has_no_goal_knowledge_semantic_diagnostics() -> None:
    # Compile must stay structural: no goal/knowledge text matching, no task-semantic
    # diagnostic symbols. New-case business shape belongs in eval contracts / prompts.
    assert not hasattr(coding_planner, "_semantic_contract_diagnostics")
    for name in (
        "SEMANTIC_CANDIDATE_FILTER_REQUIRED",
        "USER_VALUE_CASE_CHANGED",
        "OWNED_MEMBER_MUST_BE_NESTED",
        "EXPLICIT_INTERFACE_TERM_REQUIRED",
    ):
        assert name not in coding_planner.__dict__.get("__doc__", "")
        assert not hasattr(coding_planner, name)

    evaluate_params = inspect.signature(coding_planner._evaluate_source).parameters
    assert "goal" not in evaluate_params
    assert "knowledge" not in evaluate_params

    validate_params = inspect.signature(coding_sandbox.validate_code).parameters
    assert list(validate_params) == ["source"]


def test_router_semantic_supplement_preserves_raw_goal_in_generator_context() -> None:
    llm = _SequenceLLM(f"```python\n{GOOD_PROGRAM}\n```")

    generate_code(
        "ambiguous source wording",
        resolution=IntentResolution(semantic_supplement="missing semantic relationship"),
        fixture=_fixture(),
        llm=llm,
    )

    generation_text = "\n".join(
        str(message.content)
        for message in llm.messages[0]
    )
    assert "ambiguous source wording" in generation_text
    assert "missing semantic relationship" in generation_text
    assert "original user task remains authoritative" in generation_text


def test_program_owns_full_then_short_phrase_query_branch() -> None:
    source = """
def run(ctx, state):
    state = ctx.reach(state, "Open products", success={
        "entity": "Products", "fields": ["Name"],
    })
    scope = ctx.query(state, entity="Products",
        filters={"Name": "Aurora trail jacket"},
    )
    rows = ctx.acquire(scope, fields=["Name"])
    if not rows:
        scope = ctx.query(state, entity="Products",
            filters={"Name": "Aurora"},
        )
        rows = ctx.acquire(scope, fields=["Name"])
    return rows
"""
    result = execute_code(
        source,
        FixtureSpec(lookups={"products": [
            {"Name": "Aurora trail 1/4 jacket waterproof"},
            {"Name": "Aurora trail 1/4 jacket waterproof"},
            {"Name": "Aurora casual jacket"},
        ]}),
    )

    assert result.ok
    assert len(result.return_value) == 3
    # ctx.acquire also traces as op="query"; filters identify the ctx.query calls.
    queries = [
        event for event in result.trace
        if event.op == "query" and "filters" in event.kwargs
    ]
    assert [event.kwargs["filters"] for event in queries] == [
        {"Name": "Aurora trail jacket"},
        {"Name": "Aurora"},
    ]
def test_query_rejects_invented_match_mode_argument() -> None:
    source = """
def run(ctx):
    ctx.reach("Open products", success={
        "entity": "Products",
    })
    return ctx.query(entity="Products", fields=["ID"],
        match={
            "field": "Name",
            "mention": "Aurora trail jacket",
            "mode": "approximate",
            "search_key": "Aurora",
        },
    )
"""

    diagnostics = validate_code(source)
    assert any(
        item.code == "CTX_SIGNATURE" and "match" in item.message
        for item in diagnostics
    )
