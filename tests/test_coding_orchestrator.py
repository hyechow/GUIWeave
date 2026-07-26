from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.core.coding_orchestrator import (
    CodingCompileError,
    CodingProgram,
    CodingProgramRuntime,
    FixtureSpec,
    generate_reviewed_code,
    program_from_plan,
)
from gui_agent.core.coding_orchestrator.models import CodingAttempt, CodingPlan, CodingReview
from gui_agent.core.coding_orchestrator.planner import (
    _decode_review_response,
    _select_local_repair,
)
from gui_agent.core.coding_orchestrator.sandbox import (
    build_probe_fixture,
    execute_code,
    validate_code,
    validate_fixture_contract,
    validate_projection_contract,
    validate_runtime_dataflow,
)
from gui_agent.core.orchestrator.program import Acquire, Interact, Read
from gui_agent.core.schemas import (
    CollectionIntent,
    StatementOutcome,
)


GOOD_PROGRAM = """
def run(ctx):
    products_state = ctx.gui(
        "Open the Sahara leggings collection",
        success={
            "entity": "Sahara leggings",
            "fields": ["id", "Name"],
        },
    )
    products = ctx.query(
        products_state,
        entity="Sahara leggings",
        field="Name",
        fallback="Sahara",
        fields=["id", "Name"],
    )
    assert products, "Sahara products are required"
    for product in products:
        detail = ctx.read(products_state, target=product, fields=["Price"])
        new_price = round(detail["Price"] * 0.8, 2)
        assert new_price < detail["Price"], "price must decrease"
        ctx.write(
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
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        return _response(self.responses.pop(0))


def test_validate_code_accepts_normal_python_client_program() -> None:
    assert validate_code(GOOD_PROGRAM) == []


@pytest.mark.parametrize("method", ["lookup", "acquire", "interact", "compute"])
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
            "    ctx.write('save', values={'tags': {'a'}})\n"
            "    assert ctx, 'runtime exists'",
            "CTX_JSON_VALUE",
        ),
        (
            "def run(ctx):\n"
            "    ctx.gui('open', values={'Status': 'Complete'})\n"
            "    assert ctx, 'runtime exists'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n"
            "    ctx.gui('open')\n"
            "    assert ctx, 'runtime exists'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n"
            "    success = {'entity': 'Records', 'fields': ['ID']}\n"
            "    ctx.gui('open records', success=success)\n"
            "    assert ctx, 'runtime exists'",
            "GUI_SUCCESS_CONTRACT",
        ),
        (
            "def run(ctx):\n"
            "    ctx.gui('open records', success={'kind': 'done', 'name': 'Records'})\n"
            "    assert ctx, 'runtime exists'",
            "GUI_SUCCESS_CONTRACT",
        ),
        (
            "def run(ctx):\n"
            "    ctx.write('save', values={})\n"
            "    assert ctx, 'runtime exists'",
            "WRITE_VALUES_REQUIRED",
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

    orders_state = ctx.gui(
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


def test_execute_code_rejects_misaligned_gui_query_state() -> None:
    source = """
def run(ctx):
    state = ctx.gui(
        "Open records",
        success={"entity": "Records", "fields": ["ID"]},
    )
    return ctx.query(state, entity="Records", fields=["ID", "Status"])
"""

    result = execute_code(source, FixtureSpec())

    assert not result.ok
    assert "collection state does not satisfy ctx.query" in result.error


def test_validate_code_does_not_require_an_unnecessary_assertion() -> None:
    source = """
def run(ctx):
    orders_state = ctx.gui(
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


def test_fixture_contract_requires_gui_context_for_absent_query_source() -> None:
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


def test_execute_code_filters_normalizes_and_returns_query_rows() -> None:
    source = """
def run(ctx):
    orders_state = ctx.gui(
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
        fields=["Status", "Purchase Date", "Grand Total (Purchased)"],
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
    assert all(row["Purchase Date"].startswith("2023-") for row in result.return_value)
    assert result.trace[0].op == "gui"
    assert result.trace[1].op == "query"
    assert result.trace[1].kwargs["filters"] == {"Status": "Complete"}


def test_execute_code_write_updates_fixture_state() -> None:
    result = execute_code(GOOD_PROGRAM, _fixture())

    assert result.ok, result.error
    assert result.return_value == 1
    assert result.final_state["p1"]["Price"] == 80.0
    assert result.writes[0].target_id == "p1"
    assert result.writes[0].required_values == {"Price": 80.0}
    assert [event.op for event in result.trace] == [
        "gui",
        "query",
        "read",
        "write",
    ]


def test_fixture_query_normalizes_unique_phone_alias() -> None:
    source = """
def run(ctx):
    customer_state = ctx.gui(
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
    orders_state = ctx.gui(
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


def test_fixture_preserves_gui_write_evidence_when_later_code_fails() -> None:
    source = """
def run(ctx):
    products_state = ctx.gui(
        "Open products",
        success={
            "entity": "Products",
            "fields": ["id"],
        },
    )
    product = ctx.query(products_state, entity="Products", fields=["id"])[0]
    ctx.write("Update price", target=product, values={"Price": 80})
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
    orders_state = ctx.gui(
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


def test_runtime_query_yields_lookup_then_constrain_then_acquire() -> None:
    source = """
def run(ctx):
    orders_state = ctx.gui(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": [
                "Purchase Date",
                "Grand Total (Purchased)",
            ],
        },
    )
    rows = ctx.query(
        orders_state,
        entity="Orders",
        filters={"Status": "Complete"},
        fields=["Purchase Date", "Grand Total (Purchased)"],
    )
    assert rows, "orders are required"
    return rows[0]["Grand Total (Purchased)"]
"""
    runtime = CodingProgramRuntime.start(CodingProgram(goal="sum orders", source=source))

    # Public ctx.gui produces the verified state capability consumed by ctx.query.
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
    ]
    assert runtime.current_coding_plan_step == 1
    assert runtime.current_coding_plan_steps == 3
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
    runtime.send_outcome(StatementOutcome.completed("filter active"))

    # 3. acquire — materialize the now-constrained collection.
    assert isinstance(runtime.current.statement, Acquire)
    assert runtime.current.args["ui_state_token"] == ui_state_token
    runtime.send_outcome(StatementOutcome.completed(
        "rows acquired",
        outputs={"rows": [{
            "Purchase Date": "Jun 9, 2023 9:00:00 AM",
            "Grand Total (Purchased)": "$100.00",
        }]},
    ))

    assert runtime.finished
    assert runtime.reply == "100"


def test_runtime_write_infers_internal_statement_contract() -> None:
    source = """
def run(ctx):
    ctx.write("Update order status", target={"ID": "1"}, values={"Status": "Complete"})
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
    order_state = ctx.gui(
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
    assert isinstance(runtime.current.statement, Read)
    assert runtime.current.args["ui_state_token"] != parent_token
    child_token = runtime.current.args["ui_state_token"]
    assert (
        runtime.interpreter.run_log[-1].coding_payload["produced_state"]
        == child_token
    )
    assert runtime.current_coding_payload["state"] == child_token
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


def test_review_response_contract() -> None:
    assert _decode_review_response('{"approve": true, "edits": []}') == (
        True,
        (),
        "",
    )
    approved, edits, error = _decode_review_response(json.dumps({
        "approve": False,
        "edits": [{"search": "x", "replacement": "y"}],
    }))
    assert not approved
    assert edits == (("x", "y"),)
    assert not error


@pytest.mark.parametrize("payload", [
    '{"approve": true, "edits": [{"search": "x", "replacement": "y"}]}',
    '{"approve": false, "edits": []}',
])
def test_review_response_rejects_conflicting_approval_and_edits(payload: str) -> None:
    approved, edits, error = _decode_review_response(payload)

    assert not approved
    assert not edits
    assert error


def test_generate_reviewed_code_accepts_approved_program() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
        '{"approve": true, "edits": []}',
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


def test_generate_reviewed_code_applies_local_repair() -> None:
    bad = GOOD_PROGRAM.replace('fields=["id", "Name"]', 'fields=["id"]')
    search = 'fields=["id"]'
    replacement = 'fields=["id", "Name"]'
    llm = _SequenceLLM(
        f"```python\n{bad}\n```",
        json.dumps({
            "approve": False,
            "edits": [{"search": search, "replacement": replacement}],
        }),
    )

    plan = generate_reviewed_code(
        "discount Sahara leggings by 20%",
        fixture=_fixture(),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert replacement in plan.source
    assert plan.repaired


def test_static_diagnostics_override_incorrect_reviewer_approval() -> None:
    source = """
def run(ctx):
    orders_state = ctx.gui(
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
        '{"approve": true, "edits": []}',
        json.dumps({
            "approve": False,
            "edits": [{
                "search": (
                    '    rows = ctx.query('
                    'orders_state, entity="Orders")'
                ),
                "replacement": (
                    '    rows = ctx.query('
                    'orders_state, entity="Orders", fields=["ID"])'
                ),
            }],
        }),
    )

    plan = generate_reviewed_code(
        "count orders",
        fixture=FixtureSpec(lookups={"orders": [{"ID": "1"}]}),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.source.strip() == repaired.strip()
    assert len(llm.messages) == 3


def test_runtime_rejects_executable_but_unrepaired_review_failure() -> None:
    plan = CodingPlan(
        goal="count orders",
        source=GOOD_PROGRAM,
        attempts=[CodingAttempt(
            source=GOOD_PROGRAM,
            run=execute_code(GOOD_PROGRAM, _fixture()),
        )],
        review=CodingReview(
            text='{"approve": false, "edits": []}',
            approved=False,
            error="review rejected the program",
        ),
    )

    with pytest.raises(CodingCompileError):
        program_from_plan(plan)


def test_local_repair_discards_regressive_edit() -> None:
    source = """
def run(ctx):
    orders_state = ctx.gui(
        "Open orders",
        success={
            "entity": "Orders",
            "fields": ["ID"],
        },
    )
    rows = ctx.query(orders_state, entity="Orders", fields=["ID"])
    value = 1
    return len(rows)
"""
    repairs = (
        ("    value = 1\n", ""),
        ("    return len(rows)", "    open('bad')\n    return len(rows)"),
    )

    repaired, error, selected, _ = _select_local_repair(
        source,
        repairs,
        FixtureSpec(lookups={"orders": [{"ID": "1"}]}),
    )

    assert not error
    assert selected == (0,)
    assert "open(" not in repaired.source


def test_reviewer_receives_knowledge_and_new_api_schema() -> None:
    llm = _SequenceLLM(
        f"```python\n{GOOD_PROGRAM}\n```",
        '{"approve": true, "edits": []}',
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
