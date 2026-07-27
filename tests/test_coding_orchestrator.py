from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.core.orchestrator import (
    CodingCompileError,
    CodingProgram,
    CodingProgramRuntime,
    FixtureSpec,
    generate_reviewed_code,
    program_from_plan,
)
from gui_agent.core.orchestrator.planner import (
    _decode_review_response,
    _resolution_block,
)
from gui_agent.core.orchestrator.sandbox import (
    build_probe_fixture,
    execute_code,
    validate_code,
    validate_fixture_contract,
    validate_projection_contract,
    validate_runtime_dataflow,
)
from gui_agent.core.run.contracts import Acquire, Command, Interact, Read
from gui_agent.core.router.intent import EntityRef, IntentResolution
from gui_agent.core.schemas import (
    CollectionIntent,
    StatementOutcome,
)


GOOD_PROGRAM = """
def run(ctx):
    products_state = ctx.reach(
        "Open the Sahara leggings collection",
        success={
            "entity": "Sahara leggings",
            "fields": ["id", "Name"],
        },
    )
    products = ctx.query(
        products_state,
        entity="Sahara leggings",
        fields=["id", "Name"],
        filters={"Name": "Sahara leggings"},
    )
    assert products, "Sahara products are required"
    for product in products:
        detail = ctx.read(products_state, target=product, fields=["Price"])
        new_price = round(detail["Price"] * 0.8, 2)
        assert new_price < detail["Price"], "price must decrease"
        ctx.commit(
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


@pytest.mark.parametrize(
    "method",
    ["gui", "write", "lookup", "acquire", "interact", "compute"],
)
def test_validate_code_rejects_removed_planning_api(method: str) -> None:
    source = f"def run(ctx):\n    ctx.{method}('x')\n    assert ctx, 'runtime exists'"

    diagnostics = validate_code(source)

    assert any(item.code == "UNKNOWN_CTX_API" for item in diagnostics)


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
            "    ctx.commit('save', values={})\n"
            "    assert ctx, 'runtime exists'",
            "COMMIT_VALUES_REQUIRED",
        ),
        ("def run(ctx):\n    assert True, 'always'", "BUSINESS_ASSERTION_CONSTANT"),
    ],
)
def test_validate_code_rejects_unsafe_or_invalid_source(source: str, code: str) -> None:
    assert any(item.code == code for item in validate_code(source))


def test_validate_code_accepts_local_helpers_and_safe_imports() -> None:
    source = """
from datetime import datetime

def run(ctx):
    def newest(rows):
        return sorted(rows, key=lambda row: datetime.fromisoformat(row["Date"]), reverse=True)

    orders_state = ctx.reach(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Date"],
        },
    )
    rows = ctx.query(orders_state, entity="Orders", fields=["Date"])
    assert rows, "orders are required"
    return newest(rows)[0]["Date"]
"""

    assert validate_code(source) == []


def test_validate_code_allows_filter_fields_outside_return_projection() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open records", success={
        "entity": "Records", "fields": ["Name"],
    })
    return ctx.query(
        state, entity="Records", fields=["Name"],
        filters={"Status": "Complete"},
    )
"""

    assert validate_code(source) == []


def test_query_owns_fields_but_remains_bound_to_reach_entity() -> None:
    source = """
def run(ctx):
    state = ctx.reach(
        "Open records",
        success={"entity": "Records", "fields": ["ID"]},
    )
    return ctx.query(state, entity="Records", fields=["ID", "Status"])
"""

    fixture = FixtureSpec(lookups={
        "records": [{"ID": "1", "Status": "Complete"}],
    })

    assert execute_code(source, fixture).ok
    mismatched = execute_code(
        source.replace(
            'entity="Records", fields=["ID", "Status"]',
            'entity="Other", fields=["ID", "Status"]',
        ),
        fixture,
    )
    assert "collection state does not satisfy ctx.query" in mismatched.error


def test_coding_runtime_normalizes_executor_terminal_phase() -> None:
    outcome = CodingProgramRuntime.adapt_outcome(StatementOutcome.exhausted(
        "current call cannot establish its postcondition",
        evidence=["frame:1"],
    ))

    assert outcome.phase == "failed"
    assert outcome.evidence == ["frame:1"]


def test_validate_code_does_not_require_an_unnecessary_assertion() -> None:
    source = """
def run(ctx):
    orders_state = ctx.reach(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Status"],
        },
    )
    rows = ctx.query(orders_state, entity="Orders", fields=["Status"])
    return [row["Status"] for row in rows]
"""

    assert validate_code(source) == []


def test_projection_contract_tracks_query_rows() -> None:
    source = """
def run(ctx):
    rows = ctx.query(ui_state, entity="Orders", fields=["Status"])
    assert rows, "orders are required"
    return [row["Total"] for row in rows]
"""

    diagnostics = validate_projection_contract(source)

    assert any(item.code == "PROJECTED_FIELD_UNAVAILABLE" for item in diagnostics)


def test_runtime_dataflow_requires_consuming_read_values() -> None:
    source = """
def run(ctx):
    row = ctx.query(ui_state, entity="Products", fields=["id"])[0]
    state = ctx.read(ui_state, target=row, fields=["Price"])
    assert row, "product is required"
    return row
"""

    diagnostics = validate_runtime_dataflow(source)

    assert any(item.code == "UNUSED_RUNTIME_VALUE" for item in diagnostics)


def test_fixture_contract_checks_query_fields_for_matching_source() -> None:
    source = """
def run(ctx):
    rows = ctx.query(ui_state, entity="Orders", fields=["Missing"])
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
def run(ctx):
    rows = ctx.query(ui_state, entity="Orders", fields=["ID"])
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
    state = ctx.reach("Open reviews", success={
        "entity": "Reviews", "fields": ["Title"],
    })
    detail = ctx.read(state, target={"Title": "review"}, fields=["Nickname"])
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
def run(ctx):
    orders_state = ctx.reach(
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
    rows = ctx.query(
        orders_state,
        entity="Orders",
        filters={"Status": "Complete"},
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
def run(ctx):
    state = ctx.reach("Open orders", success={"entity": "Orders"})
    rows = ctx.query(
        state,
        entity="Orders",
        fields={"Purchase Date": "datetime"},
        filters={
            "Status": "Complete",
            "Purchase Date": {
                "from": "01/01/2023",
                "to": "05/31/2023",
            },
        },
    )
    return rows[0]["Purchase Date"].year
"""

    result = execute_code(source, build_probe_fixture(source))

    assert result.ok, result.error
    assert result.return_value == 2023


def test_fixture_query_does_not_fuzz_or_correct_literal_filter_phrase() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open products", success={"entity": "Products"})
    return ctx.query(
        state,
        entity="Products",
        fields=["Name"],
        filters={"Name": "Auroar"},
    )
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
        "read",
        "commit",
    ]


def test_fixture_query_normalizes_unique_phone_alias() -> None:
    source = """
def run(ctx):
    customer_state = ctx.reach(
        "Open the customer collection",
        success={
            "entity": "+1 205 881 2302",
            "fields": ["id", "phone"],
        },
    )
    rows = ctx.query(
        customer_state,
        entity="+1 205 881 2302",
        fields=["id", "phone"],
    )
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
def run(ctx):
    orders_state = ctx.reach(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Grand Total (Purchased)"],
        },
    )
    rows = ctx.query(
        orders_state,
        entity="Orders",
        fields=["Grand Total (Purchased)"],
    )
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
def run(ctx):
    products_state = ctx.reach(
        "Open products",
        success={
            "entity": "Products",
            "fields": ["id"],
        },
    )
    product = ctx.query(products_state, entity="Products", fields=["id"])[0]
    ctx.commit("Update price", target=product, values={"Price": 80})
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
def run(ctx):
    orders_state = ctx.reach(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["Status", "Grand Total (Purchased)"],
        },
    )
    rows = ctx.query(
        orders_state,
        entity="Orders",
        filters={"Status": "Complete"},
        fields=["Status", "Grand Total (Purchased)"],
    )
    assert rows, "orders are required"
    return sum(row["Grand Total (Purchased)"] for row in rows)
"""

    fixture = build_probe_fixture(source)
    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.return_value > 0


def test_probe_read_target_survives_query_field_merge_and_dynamic_filter() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open reviews", success={"entity": "Reviews"})
    product_filter = {"Product": "Olivia jacket"}
    products = ctx.query(
        state,
        entity="Reviews",
        fields=["Product"],
        filters=product_filter,
    )
    product = products[0]["Product"]
    rows = ctx.query(
        state,
        entity="Reviews",
        fields=["Nickname", "Product"],
        filters={"Product": product},
    )
    return [
        row["Nickname"]
        for row in rows
        if ctx.read(state, target=row, fields=["Rating"])["Rating"] <= 3
    ]
"""

    result = execute_code(source, build_probe_fixture(source))

    assert result.ok, result.error
    assert len(result.return_value) == 3


def test_runtime_query_yields_lookup_then_constrain_then_acquire() -> None:
    source = """
def run(ctx):
    orders_state = ctx.reach(
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
    rows = ctx.query(
        orders_state,
        entity="Orders",
        filters={"Status": "Complete"},
        fields={
            "Purchase Date": "datetime",
            "Grand Total (Purchased)": "number",
        },
    )
    assert rows, "orders are required"
    return rows[0]["Grand Total (Purchased)"]
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="sum orders", source=source))

    # Public ctx.reach produces the verified state capability consumed by ctx.query.
    assert isinstance(runtime.current.statement, Interact)
    assert isinstance(
        runtime.current.statement.interaction_intent,
        CollectionIntent,
    )
    assert runtime.current.statement.interaction_intent.phase == "reach"
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
    assert lookup_intent.required_fields == [
        "Purchase Date",
        "Grand Total (Purchased)",
        "status",
    ]
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 3
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
    assert runtime.current.args["ui_state_token"] == ui_state_token
    assert runtime.interpreter.run_log[-1].coding_payload["state"] == ui_state_token
    assert constrain_intent.predicates["status"].values == ["complete"]
    assert runtime.current_coding_plan_step == 2
    assert runtime.current_coding_plan_steps == 3
    assert runtime.current_coding_call_id == query_call_id
    runtime.send_outcome(StatementOutcome.completed("filter active"))

    # 3. acquire — materialize the now-constrained collection.
    assert isinstance(runtime.current.statement, Acquire)
    assert runtime.current.args["ui_state_token"] == ui_state_token
    assert runtime.current.args["field_types"] == {
        "Purchase Date": "datetime",
        "Grand Total (Purchased)": "number",
    }
    assert runtime.current_coding_call_id == query_call_id
    runtime.send_outcome(StatementOutcome.completed(
        "rows acquired",
        outputs={"rows": [{
            "Purchase Date": "Jun 9, 2023 9:00:00 AM",
            "Grand Total (Purchased)": "$100.00",
        }]},
    ))

    assert runtime.finished
    assert runtime.reply == "100"
    assert {
        record.coding_call_id for record in runtime.interpreter.run_log[1:]
    } == {query_call_id}
def test_runtime_query_without_predicates_skips_constrain() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open terms", success={"entity": "Terms"})
    return ctx.query(state, entity="Terms", fields=["Term", "Uses"])
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="list terms", source=source))

    runtime.send_outcome(StatementOutcome.completed("terms available"))
    assert runtime.current.statement.interaction_intent.phase == "locate"
    query_call_id = runtime.current_coding_call_id
    assert runtime.current_coding_plan_steps == 2
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
    assert runtime.current_coding_plan_step == 2
    assert runtime.current_coding_plan_steps == 2
    assert runtime.current_coding_call_id == query_call_id
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


def test_runtime_program_explicitly_branches_from_full_to_short_phrase() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open products", success={"entity": "Products"})
    rows = ctx.query(
        state, entity="Products", fields=["Name"],
        filters={"Name": "Aurora jacket"},
    )
    if not rows:
        rows = ctx.query(
            state, entity="Products", fields=["Name"],
            filters={"Name": "Aurora"},
        )
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
    runtime.send_outcome(StatementOutcome.completed("exact filter active"))
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

    runtime.send_outcome(StatementOutcome.completed("short-phrase filter active"))
    runtime.send_outcome(StatementOutcome.completed(
        "short-phrase rows acquired",
        outputs={"rows": [{"Name": "Aurora jacket waterproof"}]},
    ))

    assert runtime.finished
    assert "Aurora jacket waterproof" in runtime.reply


def test_runtime_commit_infers_internal_statement_contract() -> None:
    source = """
def run(ctx):
    ctx.commit("Update order status", target={"ID": "1"}, values={"Status": "Complete"})
    assert ctx, "runtime exists"
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="update order", source=source))

    statement = runtime.current.statement
    assert isinstance(statement, Interact)
    assert statement.goal == "Update order status"
    assert statement.required_values == {"Status": "Complete"}
    assert statement.persistence == "explicit_commit"
    assert runtime.current.inputs["target"] == {"ID": "1"}


def test_runtime_read_target_remains_one_public_call_with_internal_focus() -> None:
    source = """
def run(ctx):
    order_state = ctx.reach(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["ID"],
        },
    )
    state = ctx.read(order_state, target={"ID": "1"}, fields=["Status"])
    assert state["Status"], "status is required"
    return state["Status"]
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="read order", source=source))

    assert isinstance(
        runtime.current.statement.interaction_intent,
        CollectionIntent,
    )
    assert runtime.current.statement.interaction_intent.phase == "reach"
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
    assert runtime.reply == "Complete"


def test_runtime_read_uses_unique_row_url_as_deterministic_transport() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open reviews", success={"entity": "Reviews"})
    return ctx.read(
        state,
        target={"ID": "351", "Action_url": "https://example.test/reviews/351"},
        fields={"Rating": "number"},
    )
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="read review", source=source))

    runtime.send_outcome(StatementOutcome.completed("reviews available"))
    assert isinstance(runtime.current.statement, Command)
    assert runtime.current.statement.capability == "open_url"
    assert runtime.current.args["url"] == "https://example.test/reviews/351"
    runtime.send_outcome(StatementOutcome.completed("detail opened"))
    assert isinstance(runtime.current.statement, Read)
    assert runtime.current.args["field_types"] == {"Rating": "number"}
    runtime.send_outcome(StatementOutcome.completed(
        "rating read",
        outputs={"Rating": "3 stars"},
    ))
    assert runtime.reply == '{"Rating": 3}'


def test_review_response_contract() -> None:
    assert _decode_review_response('{"approve": true, "issues": []}') == (
        True,
        (),
        "",
    )
    approved, issues, error = _decode_review_response(json.dumps({
        "approve": False,
        "issues": [{"code": "WRONG_RESULT", "message": "x is not y"}],
    }))
    assert not approved
    assert len(issues) == 1
    assert issues[0].code == "WRONG_RESULT"
    assert not error


@pytest.mark.parametrize("payload", [
    '{"approve": true, "issues": [{"code": "X", "message": "bad"}]}',
    '{"approve": false, "issues": []}',
])
def test_review_response_rejects_conflicting_approval_and_issues(payload: str) -> None:
    approved, issues, error = _decode_review_response(payload)

    assert not approved
    assert not issues
    assert error


def test_generate_reviewed_code_accepts_approved_program() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
        '{"approve": true, "issues": []}',
    )

    plan = generate_reviewed_code(
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
        "review_started",
        "review_completed",
        "finalized",
    ]


def test_reviewer_provider_error_retries_without_regenerating_program() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
        RuntimeError("temporary length limit"),
        '{"approve": true, "issues": []}',
    )

    plan = generate_reviewed_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert len(plan.attempts) == 1
    assert len(llm.messages) == 3


def test_reviewer_unavailable_is_not_a_negative_gate() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
        RuntimeError("provider unavailable"),
        RuntimeError("provider unavailable"),
    )

    plan = generate_reviewed_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.review is not None and plan.review.unavailable
    assert len(plan.attempts) == 1


def test_generate_reviewed_code_regenerates_whole_program_once() -> None:
    bad = GOOD_PROGRAM.replace('fields=["Price"]', 'fields=["Missing"]')
    llm = _SequenceLLM(
        f"```python\n{bad}\n```",
        json.dumps({
            "approve": False,
            "issues": [{
                "code": "MISSING_READ_FIELD",
                "message": "Price must be read",
            }],
        }),
        f"```python\n{GOOD_PROGRAM}\n```",
        '{"approve": true, "issues": []}',
    )

    plan = generate_reviewed_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.source.strip() == GOOD_PROGRAM.strip()
    assert plan.repaired
    assert len(plan.attempts) == 2
    regeneration_prompt = "\n".join(
        str(message.content) for message in llm.messages[2]
    )
    assert "complete replacement program" in regeneration_prompt


def test_final_review_is_advisory_after_bounded_regeneration() -> None:
    invalid = GOOD_PROGRAM.replace(
        'fields=["Price"]',
        'fields=["Missing"]',
    )
    llm = _SequenceLLM(
        f"```python\n{invalid}\n```",
        json.dumps({
            "approve": False,
            "issues": [{"code": "RECHECK", "message": "regenerate once"}],
        }),
        f"```python\n{GOOD_PROGRAM}\n```",
        json.dumps({
            "approve": False,
            "issues": [{
                "code": "FALSE_POSITIVE",
                "message": "probabilistic audit disagrees",
            }],
        }),
    )

    plan = generate_reviewed_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.executable
    assert plan.requirements_satisfied
    assert plan.review is not None and not plan.review.approved
    assert len(plan.attempts) == 2


def test_static_diagnostics_override_incorrect_reviewer_approval() -> None:
    source = """
def run(ctx):
    orders_state = ctx.reach(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["ID"],
        },
    )
    rows = ctx.query(orders_state, entity="Orders")
    return len(rows)
"""
    repaired = source.replace(
        '    rows = ctx.query(orders_state, entity="Orders")',
        (
            '    rows = ctx.query('
            'orders_state, entity="Orders", fields=["ID"])'
        ),
    )
    llm = _SequenceLLM(
        f"```python\n{source}\n```",
        '{"approve": true, "issues": []}',
        f"```python\n{repaired}\n```",
        '{"approve": true, "issues": []}',
    )

    plan = generate_reviewed_code(
        "count orders",
        fixture=FixtureSpec(lookups={"orders": [{"ID": "1"}]}),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.source.strip() == repaired.strip()
    assert len(llm.messages) == 4


def test_reviewer_rejection_requires_valid_regenerated_program() -> None:
    invalid = GOOD_PROGRAM.replace(
        'fields=["Price"]',
        'fields=["Missing"]',
    )
    llm = _SequenceLLM(
        f"```python\n{invalid}\n```",
        json.dumps({
            "approve": False,
            "issues": [{"code": "WRONG_RESULT", "message": "result is wrong"}],
        }),
        "def run(ctx):\n    return missing",
        json.dumps({
            "approve": False,
            "issues": [{"code": "UNDEFINED", "message": "missing is undefined"}],
        }),
    )

    plan = generate_reviewed_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert not plan.requirements_satisfied
    with pytest.raises(CodingCompileError):
        program_from_plan(plan)


def test_reviewer_receives_knowledge_and_new_api_schema() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
        '{"approve": true, "issues": []}',
    )

    generate_reviewed_code(
        "discount Sahara leggings",
        knowledge="Product Price is available on the product detail page.",
        fixture=_fixture(),
        llm=llm,
    )

    review_text = "\n".join(
        str(message.content)
        for message in llm.messages[1]
    )
    assert "Product Price is available" in review_text
    assert "collection sources" in review_text


def test_router_search_key_is_an_explicit_literal_query_branch() -> None:
    block = _resolution_block(IntentResolution(entities=[EntityRef(
        mention="Aurora jacket",
        search_key="Aurora",
    )]))

    assert block is not None
    assert "not a required standalone collection" in block.content
    assert "query the full mention first" in block.content
    assert "strict literal queries" in block.content
    assert "match_mode" not in block.content


def test_program_owns_full_then_short_phrase_query_branch() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open products", success={
        "entity": "Products", "fields": ["Name"],
    })
    rows = ctx.query(
        state, entity="Products", fields=["Name"],
        filters={"Name": "Aurora trail jacket"},
    )
    if not rows:
        rows = ctx.query(
            state, entity="Products", fields=["Name"],
            filters={"Name": "Aurora"},
        )
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
    queries = [event for event in result.trace if event.op == "query"]
    assert [event.kwargs["filters"] for event in queries] == [
        {"Name": "Aurora trail jacket"},
        {"Name": "Aurora"},
    ]


def test_query_rejects_invented_match_mode_argument() -> None:
    source = """
def run(ctx):
    state = ctx.reach("Open products", success={
        "entity": "Products",
    })
    return ctx.query(
        state, entity="Products", fields=["ID"],
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
