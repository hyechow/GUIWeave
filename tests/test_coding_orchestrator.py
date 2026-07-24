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
from gui_agent.core.coding_orchestrator.models import (
    CodingAttempt,
    CodingPlan,
    CodingReview,
)
from gui_agent.core.orchestrator.program import Acquire, Interact, Read
from gui_agent.core.schemas import StatementOutcome
from gui_agent.core.coding_orchestrator.planner import _decode_review_response
from gui_agent.core.coding_orchestrator.sandbox import (
    execute_code,
    validate_code,
    validate_fixture_contract,
    validate_projection_contract,
    validate_runtime_dataflow,
)


GOOD_PROGRAM = '''
def run(ctx):
    scope = ctx.lookup("Sahara leggings", field="name", fallback="Sahara")
    products = ctx.acquire(scope, fields=["id", "name"], coverage="complete")
    assert products, "Sahara product candidates must exist"
    changed = 0
    for product in products:
        state = ctx.read(product, fields=["size", "price"])
        if state["size"] != "28":
            continue
        new_price = round(state["price"] * 0.865, 2)
        assert new_price == round(state["price"] * 0.865, 2), "discount price must use requested precision"
        saved = ctx.interact(
            "save the discounted price for this product",
            success="the product price is durably saved",
            inputs={"product": product},
            required_values={"price": new_price},
            persistence="explicit_commit",
        )
        assert saved, "the discounted product price must be saved"
        changed += 1
    assert changed > 0, "at least one size 28 product must be updated"
'''


def _fixture() -> FixtureSpec:
    return FixtureSpec(
        lookups={
            "sahara leggings": [
                {"id": "p1", "name": "Sahara A"},
                {"id": "p2", "name": "Sahara B"},
            ],
        },
        reads={
            "p1": {"size": "28", "price": 100.0},
            "p2": {"size": "30", "price": 80.0},
        },
    )


def test_validate_code_accepts_normal_python_control_and_data_flow() -> None:
    assert validate_code(GOOD_PROGRAM) == []


def test_validate_code_accepts_keyword_only_capability_arguments() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("items", field="name", fallback="item")
    rows = ctx.acquire(scope, fields=["id"], coverage="complete")
    if rows:
        state = ctx.read(rows[0], fields=["value"])
        assert "value" in state, "the requested value must be readable"
        return state
'''
    assert validate_code(source) == []


def test_validate_code_accepts_safe_local_string_date_and_sort_computation() -> None:
    source = '''
def run(ctx):
    import datetime
    values = ["2022-12-01", "2022-05-01"]
    values.sort()
    first = datetime.datetime.strptime(values[0].strip(), "%Y-%m-%d")
    month = first.strftime("%B").lower().replace("may", "May")
    assert first.year == 2022 and first.month == 5, "the first date must be May 2022"
    return {"month": month, "parts": values[0].split("-")}
'''
    assert validate_code(source) == []
    result = execute_code(source, FixtureSpec())
    assert result.ok, result.error
    assert result.return_value == {"month": "May", "parts": ["2022", "05", "01"]}


def test_validate_code_accepts_safe_top_level_import_lambda_and_local_helper() -> None:
    source = '''
from datetime import datetime

def run(ctx):
    def parse(value):
        return datetime.strptime(value, "%Y-%m-%d")
    rows = [{"date": "2022-05-01"}, {"date": "2022-12-01"}]
    rows = sorted(rows, key=lambda row: parse(row["date"]), reverse=True)
    assert rows[0]["date"] == "2022-12-01", "rows must be sorted by parsed date"
    return rows
'''
    assert validate_code(source) == []
    result = execute_code(source, FixtureSpec())
    assert result.ok, result.error


def test_validate_code_accepts_calling_a_safe_imported_date_constructor() -> None:
    source = '''
from datetime import date

def run(ctx):
    today = date(2026, 7, 23)
    assert today.year == 2026, "the deterministic date must be constructed"
    return today.strftime("%Y-%m-%d")
'''

    assert validate_code(source) == []
    result = execute_code(source, FixtureSpec())
    assert result.ok, result.error
    assert result.return_value == "2026-07-23"


def test_validate_fixture_contract_reports_only_globally_unavailable_literal_fields() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("Product Attributes")
    rows = ctx.acquire(scope, fields=["attribute_code", "default_label"])
    assert rows, "an attribute must exist"
    return ctx.read(rows[0], fields=["options"])
'''
    fixture = FixtureSpec(
        lookups={"product attributes": [{"attribute_code": "size", "label": "Size"}]},
        reads={"size": {"options": []}},
    )

    diagnostics = validate_fixture_contract(source, fixture)

    assert [diagnostic.code for diagnostic in diagnostics] == ["MOCK_FIELD_UNAVAILABLE"]
    assert "default_label" in diagnostics[0].message
    assert "options" not in diagnostics[0].message


def test_validate_projection_contract_tracks_records_through_loop_and_list_selection() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("products")
    rows = ctx.acquire(scope, fields=["type", "sku"])
    simple = [row for row in rows if row.get("type") == "Simple Product"]
    assert simple, "a simple product must exist"
    ordered = sorted(simple, key=lambda row: row["sku"])
    target = ordered[0]
    return target.get("id")
'''

    diagnostics = validate_projection_contract(source)

    assert [diagnostic.code for diagnostic in diagnostics] == [
        "PROJECTED_FIELD_UNAVAILABLE",
    ]
    assert "'id'" in diagnostics[0].message


def test_static_review_rejects_undefined_names_and_unprojected_generator_fields() -> None:
    undefined_source = '''
def run(ctx):
    scope = ctx.lookup("Orders")
    records = ctx.acquire(scope, fields=["ID", "Grand Total (Purchased)"])
    assert records, "orders must exist"
    last_two = records[:2]
    last_two = sorted_records[:2]
    return last_two
'''
    projection_source = undefined_source.replace(
        "    last_two = sorted_records[:2]\n    return last_two",
        '    return sum(float(row["Total"]) for row in last_two)',
    )

    assert {diagnostic.code for diagnostic in validate_code(undefined_source)} == {
        "UNDEFINED_NAME",
    }
    assert {
        diagnostic.code
        for diagnostic in validate_projection_contract(projection_source)
    } == {"PROJECTED_FIELD_UNAVAILABLE"}


@pytest.mark.parametrize(
    ("expression", "expected_codes"),
    [
        ("new_quantity = current_quantity + 5", []),
        ("new_quantity = 5", ["UNUSED_RUNTIME_VALUE"]),
    ],
)
def test_validate_runtime_dataflow_requires_consuming_read_values(
    expression: str,
    expected_codes: list[str],
) -> None:
    source = f'''
def run(ctx):
    state = ctx.read({{"sku": "MP12-33-Blue"}}, fields=["quantity"])
    current_quantity = state["quantity"]
    {expression}
    assert new_quantity >= 0, "quantity must remain nonnegative"
    saved = ctx.interact(
        "save quantity",
        success="quantity is saved",
        required_values={{"quantity": new_quantity}},
        persistence="explicit_commit",
    )
    assert saved, "quantity must be saved"
'''

    assert [
        diagnostic.code
        for diagnostic in validate_runtime_dataflow(source)
    ] == expected_codes
    if expected_codes:
        message = validate_runtime_dataflow(source)[0].message
        assert "consume it in the requested calculation" in message
        assert "otherwise delete the entire unnecessary read" in message


def test_validate_code_accepts_normal_raise_and_string_formatting() -> None:
    source = '''
def run(ctx):
    value = 1
    if value < 0:
        raise ValueError("negative value")
    assert value > 0, "value must be positive"
    return "value={}".format(value)
'''

    assert validate_code(source) == []
    result = execute_code(source, FixtureSpec())
    assert result.ok, result.error
    assert result.return_value == "value=1"


def test_validate_code_accepts_asserted_durable_interaction() -> None:
    source = '''
def run(ctx):
    assert ctx.interact(
        "save status",
        success="status is saved",
        required_values={"status": "Complete"},
        persistence="explicit_commit",
    ), "status must be saved"
'''

    assert validate_code(source) == []


def test_validate_code_allows_algorithmic_break_that_does_not_select_business_identity() -> None:
    source = '''
def run(ctx):
    parsed = None
    for value in ["invalid", "2026-07-23"]:
        if value == "2026-07-23":
            parsed = value
            break
    assert parsed is not None, "one supported value must parse"
    return parsed
'''

    assert validate_code(source) == []


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("import os\ndef run(ctx):\n    pass", "UNSAFE_IMPORT"),
        ("def run(ctx):\n    while True:\n        pass", "UNSAFE_SYNTAX"),
        (
            "def run(ctx):\n"
            "    scope = ctx.lookup('orders')\n"
            "    rows = ctx.acquire(scope, fields=['id'])\n"
            "    for row in rows:\n"
            "        break\n"
            "    assert rows, 'orders must exist'",
            "BUSINESS_IDENTITY_FIRST_MATCH",
        ),
        ("def run(ctx):\n    ctx.delete_everything()", "UNKNOWN_CTX_API"),
        ("def run(ctx):\n    return ctx.__class__", "DUNDER_ACCESS"),
        ("def helper():\n    pass\ndef run(ctx):\n    pass", "ENTRYPOINT"),
        ("def run(ctx):\n    eval('1 + 1')", "UNSAFE_CALL"),
        ("def run(ctx):\n    import os\n    assert os, 'module exists'", "UNSAFE_IMPORT"),
        (
            "def run(ctx):\n    scope = ctx.lookup('order', '302')\n    assert scope, 'scope exists'",
            "CTX_SIGNATURE",
        ),
        ("def run(ctx):\n    ctx.lookup('entity', unknown=True)", "CTX_SIGNATURE"),
        (
            "def run(ctx):\n"
            "    scope = ctx.lookup('orders')\n"
            "    assert scope, 'scope exists'",
            "LOOKUP_SCOPE_UNUSED",
        ),
        (
            "def run(ctx):\n"
            "    ctx.lookup('orders')\n"
            "    rows = ctx.acquire(ctx.lookup('orders'), fields=['id'])\n"
            "    assert rows, 'orders exist'",
            "LOOKUP_SCOPE_UNUSED",
        ),
        (
            "def run(ctx):\n"
            "    rows = ctx.acquire({'entity': 'orders'}, fields=['id'])\n"
            "    assert rows, 'orders exist'",
            "ACQUIRE_SCOPE_ORIGIN",
        ),
        ("def run(ctx):\n    return 'done'", "BUSINESS_ASSERTION_REQUIRED"),
        ("def run(ctx):\n    assert True, 'always'", "BUSINESS_ASSERTION_CONSTANT"),
        ("def run(ctx):\n    value = 1\n    assert value", "BUSINESS_ASSERTION_MESSAGE"),
        (
            "def run(ctx):\n    ok = ctx.interact('filter rows', success='rows are filtered', persistence='immediate_commit')\n    assert ok, 'filter applied'",
            "INTERACT_PERSISTENCE",
        ),
        (
            "def run(ctx):\n    ok = ctx.interact('filter rows')\n    assert ok, 'filter applied'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n    ok = ctx.interact('filter rows', goal='filter records', success='rows are filtered')\n    assert ok, 'filter applied'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n    ok = ctx.interact('save', success='saved', target={'id': '1'}, values={'status': 'done'})\n    assert ok, 'saved'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n    ok = ctx.interact('save', success=True)\n    assert ok, 'saved'",
            "CTX_SIGNATURE",
        ),
        (
            "def run(ctx):\n    ok = ctx.interact('save', success='saved', required_values={}, persistence='explicit_commit')\n    assert ok, 'saved'",
            "INTERACT_REQUIRED_VALUES",
        ),
        (
            "def run(ctx):\n    value = 1\n    assert value, 'value exists'\n    ctx.interact('save', success='saved', required_values={'status': 'Complete'}, persistence='explicit_commit')",
            "INTERACT_RESULT_UNUSED",
        ),
        (
            "def run(ctx):\n"
            "    groups = ['General', 'Wholesale']\n"
            "    assert groups, 'groups must exist'\n"
            "    saved = ctx.interact('save groups', success='groups are saved', "
            "required_values={'groups': set(groups)}, persistence='explicit_commit')\n"
            "    assert saved, 'groups must be saved'",
            "INTERACT_JSON_VALUE",
        ),
        (
            "from datetime import date\n"
            "def run(ctx):\n"
            "    today = date(2026, 7, 23)\n"
            "    previous = date(today.year, today.month, 0)\n"
            "    assert previous < today, 'previous date must be earlier'\n"
            "    return previous",
            "INVALID_DATE_CONSTRUCTION",
        ),
        (
            "def run(ctx):\n    rows = ctx.acquire(scope=None, fields=['id'])\n    assert rows, 'rows required'",
            "ACQUIRE_SCOPE_REQUIRED",
        ),
        (
            "def run(ctx):\n"
            "    reports = ctx.lookup('Reports')\n"
            "    sales = ctx.lookup(reports, fallback='Sales Reports')\n"
            "    assert sales, 'sales scope must exist'",
            "LOOKUP_ENTITY_REQUIRED",
        ),
    ],
)
def test_validate_code_rejects_unsafe_or_nonminimal_source(source: str, code: str) -> None:
    assert code in {diagnostic.code for diagnostic in validate_code(source)}


def test_execute_code_runs_fixture_and_records_concrete_effect_trace() -> None:
    result = execute_code(GOOD_PROGRAM, _fixture())

    assert result.ok, result.error
    assert [event.op for event in result.trace] == [
        "lookup", "acquire", "read", "interact", "read",
    ]
    interaction = next(event for event in result.trace if event.op == "interact")
    assert interaction.kwargs["inputs"]["product"]["id"] == "p1"
    assert interaction.kwargs["required_values"]["price"] == 86.5
    assert interaction.kwargs["persistence"] == "explicit_commit"
    assert interaction.result is True
    assert len(result.writes) == 1
    write = result.writes[0]
    assert write.target_id == "p1"
    assert write.success == "the product price is durably saved"
    assert write.inputs["product"]["id"] == "p1"
    assert write.required_values == {"price": 86.5}
    assert write.persistence == "explicit_commit"
    assert write.before["price"] == 100.0
    assert write.after["price"] == 86.5
    assert write.applied
    assert result.final_state["p1"]["price"] == 86.5


def test_execute_code_returns_source_line_for_fixture_failure() -> None:
    source = '''
def run(ctx):
    target = {"id": "missing"}
    assert target, "target identity must exist"
    ctx.read(target, fields=["price"])
'''
    result = execute_code(source, _fixture())

    assert not result.ok
    assert "<coding-plan>" in result.error
    assert "no fixture read state" in result.error


def test_execute_code_resolves_projected_record_by_stable_sku_alias() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("Sahara leggings")
    products = ctx.acquire(scope, fields=["sku"], coverage="complete")
    assert products, "Sahara product candidates must exist"
    state = ctx.read(products[0], fields=["price"])
    assert state["price"] > 0, "the current price must be readable"
'''
    fixture = FixtureSpec(
        lookups={"sahara leggings": [{"id": "p1", "sku": "WP06-28"}]},
        reads={"p1": {"price": 100.0}},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error


def test_fixture_lookup_normalizes_a_unique_phone_number_alias() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("+1 2058812302")
    customers = ctx.acquire(scope, fields=["name", "email", "phone"])
    assert customers, "the customer must exist"
    return customers
'''
    fixture = FixtureSpec(lookups={
        "8812302": [{
            "id": "c1",
            "name": "Avery Stone",
            "email": "avery@example.test",
            "phone": "+1 (205) 881-2302",
        }],
    })

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.return_value == [{
        "name": "Avery Stone",
        "email": "avery@example.test",
        "phone": "+1 (205) 881-2302",
    }]


def test_fixture_reports_unavailable_collection_fields_for_review() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(
        ctx.lookup("orders"),
        fields=["name", "email"],
        coverage="complete",
    )
    assert rows, "orders must exist"
'''
    fixture = FixtureSpec(lookups={
        "orders": [{"id": "order-1", "customer_name": "Sarah Miller", "status": "Pending"}],
    })

    result = execute_code(source, fixture)

    assert not result.ok
    assert "collection fields ['name', 'email'] are unavailable" in result.error
    assert "available_fields=['customer_name', 'id', 'status']" in result.error


def test_fixture_records_statement_transition_without_database_target_resolution() -> None:
    source = '''
def run(ctx):
    target = {"id": "missing"}
    assert target, "target identity must exist"
    saved = ctx.interact(
        "save a value",
        success="the value is saved",
        inputs={"record": target},
        required_values={"status": "Complete"},
        persistence="explicit_commit",
    )
    assert saved, "the write call must complete"
'''

    result = execute_code(source, FixtureSpec())

    assert result.ok, result.error
    assert len(result.writes) == 1
    assert result.writes[0].target_id is None
    assert result.writes[0].required_values == {"status": "Complete"}
    assert result.writes[0].applied
    assert result.trace[-1].result is True
    assert result.writes[0].before == {}
    assert result.writes[0].after == {"status": "Complete"}


def test_coding_runtime_yields_real_statement_invocations_and_resumes_dataflow() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("order 301", field="order_id")
    rows = ctx.acquire(scope, fields=["id", "status"])
    assert len(rows) == 1, "order 301 must be unique"
    state = ctx.read(rows[0], fields=["quantity"])
    updated = state["quantity"] + 5
    assert updated == 12, "quantity must increase by five"
    saved = ctx.interact(
        "save inventory",
        success="inventory quantity is saved",
        inputs={"order": rows[0]},
        required_values={"quantity": updated},
        persistence="explicit_commit",
    )
    assert saved, "inventory must be saved"
    return updated
'''
    runtime = CodingProgramRuntime.start(CodingProgram(goal="update inventory", source=source))
    try:
        assert isinstance(runtime.current.statement, Interact)
        scope = {
            "kind": "resolved_collection",
            "entity": "order 301",
            "surface_fingerprint": "table:#orders",
            "available_fields": ["id", "status"],
        }
        runtime.send_outcome(StatementOutcome.completed(
            "scope established",
            outputs={"scope": scope},
        ))

        assert isinstance(runtime.current.statement, Acquire)
        assert runtime.current.args["lookup_scope"] == scope
        runtime.send_outcome(StatementOutcome.completed(
            "rows acquired",
            outputs={"rows": [{"id": "o301", "status": "Pending"}]},
        ))

        assert isinstance(runtime.current.statement, Interact)
        assert runtime.current.inputs == {
            "target": {"id": "o301", "status": "Pending"},
        }
        runtime.send_outcome(StatementOutcome.completed("detail exposed"))

        assert isinstance(runtime.current.statement, Read)
        runtime.send_outcome(StatementOutcome.completed(
            "quantity read",
            outputs={"quantity": 7},
        ))

        assert isinstance(runtime.current.statement, Interact)
        assert runtime.current.statement.required_values == {"quantity": 12}
        runtime.send_outcome(StatementOutcome.completed("inventory saved"))

        assert runtime.finished
        assert runtime.reply == "12"
        assert runtime.interpreter.env["return"] == 12
        assert len(runtime.interpreter.run_log) == 5
    finally:
        runtime.close()


def test_fixture_preserves_write_evidence_when_later_code_fails() -> None:
    source = '''
def run(ctx):
    products = ctx.acquire(
        ctx.lookup("Sahara leggings"),
        fields=["sku"],
        coverage="complete",
    )
    assert products, "a product must exist"
    saved = ctx.interact(
        "save product price",
        success="the product price is saved",
        inputs={"product": products[0]},
        required_values={"price": 64.88},
        persistence="explicit_commit",
    )
    assert saved, "the product price must be saved"
    state = ctx.read(products[0], fields=["price"])
    assert state["price"] == 75.0, "a later business check failed"
'''
    fixture = FixtureSpec(
        lookups={"sahara leggings": [{"id": "p1", "sku": "WP06-28"}]},
        reads={"p1": {"price": 75.0}},
    )

    result = execute_code(source, fixture)

    assert not result.ok
    assert result.writes[0].target_id == "p1"
    assert result.writes[0].before["price"] == 75.0
    assert result.writes[0].after["price"] == 64.88
    assert result.final_state["p1"]["price"] == 64.88


def test_fixture_durable_interaction_updates_read_state() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("Sahara leggings")
    products = ctx.acquire(scope, fields=["sku"], coverage="complete")
    assert products, "a product must exist"
    saved = ctx.interact(
        "save product price",
        success="the product price is saved",
        inputs={"product": products[0]},
        required_values={"Price": 64.88},
        persistence="explicit_commit",
    )
    assert saved, "the product price must be saved"
    state = ctx.read(products[0], fields=["price"])
    assert state["price"] == 64.88, "the saved price must be readable"
'''
    fixture = FixtureSpec(
        lookups={"sahara leggings": [{"id": "p1", "sku": "WP06-28"}]},
        reads={"p1": {"price": 75.0}},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.writes[0].before["price"] == 75.0
    assert result.writes[0].after["price"] == 64.88
    assert result.final_state["p1"]["price"] == 64.88


def test_fixture_resolves_statement_target_from_runtime_stable_identity() -> None:
    source = '''
def run(ctx):
    products = ctx.acquire(
        ctx.lookup("Sahara leggings"),
        fields=["id"],
        coverage="complete",
    )
    assert products, "a product must exist"
    saved = ctx.interact(
        "save product price",
        success="the product price is saved",
        inputs={"product_id": products[0]["id"]},
        required_values={"price": 64.88},
        persistence="explicit_commit",
    )
    assert saved, "the product price must be saved"
'''
    fixture = FixtureSpec(
        lookups={"sahara leggings": [{"id": "p1"}]},
        reads={"p1": {"price": 75.0}},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error
    assert result.writes[0].target_id == "p1"
    assert result.writes[0].before["price"] == 75.0
    assert result.writes[0].after["price"] == 64.88
    assert result.final_state["p1"]["price"] == 64.88


def test_fixture_target_identity_skips_null_preferred_alias() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("reviews")
    reviews = ctx.acquire(scope, fields=["action_url", "title"], coverage="complete")
    assert reviews, "a review must exist"
    state = ctx.read(reviews[0], fields=["rating"])
    assert state["rating"] == 5, "the review detail must resolve by a non-null identity"
'''
    fixture = FixtureSpec(
        lookups={"reviews": [{"id": "r1", "action_url": None, "title": "Excellent"}]},
        reads={"r1": {"rating": 5}},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error


def test_fixture_semantic_fields_are_case_insensitive() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("Sahara leggings")
    products = ctx.acquire(scope, fields=["SKU", "Type"], coverage="complete")
    assert products[0]["Type"] == "Simple Product", "product type must be projected"
    state = ctx.read(products[0], fields=["Price"])
    assert state["Price"] == 100.0, "current price must be readable"
'''
    fixture = FixtureSpec(
        lookups={
            "sahara leggings": [
                {"id": "p1", "sku": "WP06-28", "type": "Simple Product"},
            ],
        },
        reads={"p1": {"price": 100.0}},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error


def test_fixture_resolves_uppercase_id_as_stable_identity() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("order 302")
    orders = ctx.acquire(scope, fields=["ID", "Status"], coverage="complete")
    assert len(orders) == 1, "order 302 must be unique"
    state = ctx.read(orders[0], fields=["Status"])
    assert state["Status"] == "Pending", "order status must be readable"
'''
    fixture = FixtureSpec(
        lookups={"order 302": [{"id": "order-302", "status": "Pending"}]},
        reads={"order-302": {"Status": "Pending"}},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error


def test_fixture_semantic_fields_normalize_spaces_and_underscores() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("orders")
    rows = ctx.acquire(scope, fields=["Customer Email"], coverage="complete")
    assert rows[0]["Customer Email"] == "amy@example.test", "customer email must be projected"
'''
    fixture = FixtureSpec(
        lookups={"orders": [{"customer_email": "amy@example.test"}]},
    )

    result = execute_code(source, fixture)

    assert result.ok, result.error


class _ContentSequenceLlm:
    def __init__(self, contents: list[str]) -> None:
        self.contents = iter(contents)
        self.calls = 0
        self.messages = []

    def invoke(self, messages):
        self.calls += 1
        self.messages.append(messages)
        return SimpleNamespace(
            content=next(self.contents),
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
        )


class _BindableContentSequenceLlm(_ContentSequenceLlm):
    def __init__(self, contents: list[str]) -> None:
        super().__init__(contents)
        self.bind_calls = []

    def bind(self, **kwargs):
        self.bind_calls.append(kwargs)
        return self


def _review_response(
    *edits: tuple[str, str],
    approve: bool = False,
) -> str:
    return json.dumps({
        "approve": approve,
        "edits": [
            {"search": search, "replacement": replacement}
            for search, replacement in edits
        ],
    })


def test_generate_reviewed_code_passes_statement_contract_evidence_to_reviewer() -> None:
    initial = (
        "def run(ctx):\n"
        "    target = {'id': 'missing'}\n"
        "    assert target, 'target identity must exist'\n"
        "    saved = ctx.interact(\n"
        "        'save status', success='status is saved', inputs={'record': target},\n"
        "        required_values={'status': 'Complete'},\n"
        "        persistence='explicit_commit')\n"
        "    assert saved, 'status must be saved'"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response((
            "target = {'id': 'missing'}",
            "target = {'id': 'record-1'}",
        )),
    ])

    plan = generate_reviewed_code(
        "complete a fixture task",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert plan.repaired
    assert plan.review is not None and not plan.review.approved
    assert plan.requirements_satisfied
    assert llm.calls == 2
    assert plan.attempts[0].run.writes[0].applied
    review_messages = "\n".join(
        str(getattr(message, "content", ""))
        for message in llm.messages[1]
    )
    assert "WriteEvent" in review_messages
    assert "required_values" in review_messages
    assert "Mock API schema" in review_messages
    assert "target = {'id': 'record-1'}" in plan.source


def test_generate_reviewed_code_skips_revision_when_reviewer_approves() -> None:
    source = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'fixture task must have work'\n"
        "    return 'done'"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{source}\n```",
        _review_response(approve=True),
    ])

    plan = generate_reviewed_code(
        "complete a fixture task",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert not plan.repaired
    assert plan.review is not None and plan.review.approved
    assert plan.requirements_satisfied
    assert plan.source == source
    assert llm.calls == 2


def test_runtime_rejects_an_executable_but_unrepaired_review_failure() -> None:
    source = "def run(ctx):\n    return []"
    plan = CodingPlan(
        goal="return the requested records",
        source=source,
        attempts=[CodingAttempt(source=source)],
        review=CodingReview(
            text='{"approve": false, "edits": [{"search": "return []", '
            '"replacement": "return missing"}]}',
            approved=False,
            edits=(("return []", "return missing"),),
        ),
    )

    assert plan.executable
    assert not plan.repaired
    assert not plan.requirements_satisfied
    with pytest.raises(
        CodingCompileError,
        match="coding review rejected the unrepaired program",
    ):
        program_from_plan(plan)


@pytest.mark.parametrize("payload", [
    '{"approve": true, "edits": [{"search": "a", "replacement": "b"}]}',
    '{"approve": false, "edits": []}',
])
def test_review_response_rejects_conflicting_approval_and_edits(payload: str) -> None:
    approved, edits, error = _decode_review_response(payload)
    assert not approved
    assert edits == ()
    assert error



def test_generate_reviewed_code_bounds_the_reviewer_output() -> None:
    source = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'fixture task must have work'\n"
        "    return 'done'"
    )
    llm = _BindableContentSequenceLlm([
        f"```python\n{source}\n```",
        _review_response(approve=True),
    ])

    plan = generate_reviewed_code(
        "complete a fixture task",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert llm.bind_calls == [{
        "max_tokens": 4096,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }]


def test_generate_reviewed_code_rejects_a_non_code_review_response() -> None:
    initial = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'fixture task must have work'\n"
        "    return 'wrong'"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        "The return value should be derived from runtime data.",
    ])

    plan = generate_reviewed_code(
        "return a runtime-derived result",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert not plan.executable
    assert not plan.requirements_satisfied
    assert llm.calls == 2


def test_generate_reviewed_code_exposes_deduplicated_mock_data_to_reviewer() -> None:
    source = "def run(ctx):\n    rows = [1]\n    assert rows, 'rows exist'"
    llm = _ContentSequenceLlm([
        f"```python\n{source}\n```",
        _review_response(approve=True),
    ])
    rows = [{"id": "o1", "status": "Pending"}]

    plan = generate_reviewed_code(
        "inspect pending orders",
        fixture=FixtureSpec(lookups={"orders": rows, "pending orders": rows}),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert llm.calls == 2
    review_messages = "\n".join(
        str(getattr(message, "content", ""))
        for message in llm.messages[1]
    )
    assert "aliases=['orders', 'pending orders']" in review_messages
    assert "available_fields=['id', 'status']" in review_messages
    assert review_messages.count("'status': ['Pending']") == 1


def test_current_view_schema_is_private_and_rejects_field_invention() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('Top Search Terms')\n"
        "    rows = ctx.acquire(scope, fields=['term', 'count'])\n"
        "    assert len(rows) >= 2, 'two terms must exist'\n"
        "    return rows"
    )
    repaired = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('Top Search Terms')\n"
        "    rows = ctx.acquire(scope, fields=['Search Term', 'Uses'])\n"
        "    assert len(rows) >= 2, 'two terms must exist'\n"
        "    return rows"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response((initial, repaired)),
    ])
    observation = SimpleNamespace(
        tables=[{
            "caption": "Top Search Terms",
            "headers": ["Search Term", "Results", "Uses"],
            "rows": [{"Search Term": "private runtime value", "Uses": "19"}],
        }],
        form_controls=[],
    )

    plan = generate_reviewed_code(
        "return the top two search terms",
        current_observation=observation,
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.repaired
    assert "fields=['Search Term', 'Uses']" in plan.source
    assert plan.attempts[0].diagnostics[0].code == "MOCK_FIELD_UNAVAILABLE"
    for messages in llm.messages:
        prompt = "\n".join(str(message.content) for message in messages)
        assert '"source": "Top Search Terms"' in prompt
        assert '"fields": ["Search Term", "Results", "Uses"]' in prompt
        assert "private runtime value" not in prompt


def test_current_view_schema_does_not_constrain_a_different_later_collection() -> None:
    source = (
        "def run(ctx):\n"
        "    entered = ctx.interact('open orders', success='orders page is open')\n"
        "    assert entered, 'orders page must open'\n"
        "    scope = ctx.lookup('Orders')\n"
        "    rows = ctx.acquire(scope, fields=['Status', 'Purchase Date', 'Grand Total'])\n"
        "    assert rows, 'orders must exist'\n"
        "    return rows"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{source}\n```",
        _review_response(approve=True),
    ])
    observation = SimpleNamespace(
        tables=[{
            "caption": "Top Search Terms",
            "headers": ["Search Term", "Uses"],
            "rows": [],
        }],
        form_controls=[],
    )

    plan = generate_reviewed_code(
        "open orders and return order fields",
        current_observation=observation,
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert not plan.attempts[0].diagnostics


def test_current_view_requires_context_transition_before_a_different_lookup() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('Orders')\n"
        "    rows = ctx.acquire(scope, fields=['Status'])\n"
        "    assert rows, 'orders must exist'\n"
        "    return rows"
    )
    repaired = initial.replace(
        "    scope =",
        "    entered = ctx.interact('open orders', success='orders page is open')\n"
        "    assert entered, 'orders page must open'\n"
        "    scope =",
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response((initial, repaired)),
    ])
    observation = SimpleNamespace(
        tables=[{
            "caption": "Top Search Terms",
            "headers": ["Search Term", "Uses"],
            "rows": [],
        }],
        form_controls=[],
    )

    plan = generate_reviewed_code(
        "open orders and return statuses",
        current_observation=observation,
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.repaired
    assert plan.attempts[0].diagnostics[0].code == "LOOKUP_CONTEXT_REQUIRED"


def test_static_diagnostics_override_an_incorrect_reviewer_approval() -> None:
    initial = (
        "def run(ctx):\n"
        "    entered = ctx.interact('open orders')\n"
        "    assert entered, 'orders must open'"
    )
    repaired = initial.replace(
        "ctx.interact('open orders')",
        "ctx.interact('open orders', success='orders page is open')",
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response(approve=True),
        _review_response((initial, repaired)),
    ])

    plan = generate_reviewed_code("open orders", llm=llm)

    assert plan.requirements_satisfied
    assert plan.repaired
    assert "success='orders page is open'" in plan.source
    assert llm.calls == 3


def test_production_review_executes_with_synthetic_probe_data() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('Orders')\n"
        "    rows = ctx.acquire(scope, fields=['ID'])\n"
        "    assert rows, 'orders must exist'\n"
        "    return 1 / 0"
    )
    repaired = initial.replace("return 1 / 0", "return len(rows)")
    review_text = _review_response((initial, repaired))
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        review_text,
    ])

    plan = generate_reviewed_code("count orders", llm=llm)

    assert plan.requirements_satisfied
    assert plan.repaired
    assert plan.attempts[0].run is not None
    assert "ZeroDivisionError" in plan.attempts[0].run.error
    assert plan.attempts[1].run.return_value >= 1


def test_generate_reviewed_code_emits_complete_auditable_event_sequence() -> None:
    initial = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'rows exist'\n"
        "    return 1 / 0"
    )
    repaired = initial.replace("return 1 / 0", "return len(rows)")
    review_text = _review_response((initial, repaired))
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        review_text,
    ])
    emitted = []

    plan = generate_reviewed_code(
        "count rows",
        llm=llm,
        on_event=emitted.append,
    )

    assert emitted == plan.events
    assert [event.kind for event in emitted] == [
        "generation_started",
        "generation_completed",
        "diagnostics",
        "probe",
        "review_started",
        "review_completed",
        "repair_completed",
        "diagnostics",
        "probe",
        "finalized",
    ]
    assert emitted[1].data["source"] == initial
    assert emitted[5].data["text"] == review_text
    assert emitted[6].data["before"] == initial
    assert emitted[6].data["after"] == repaired


def test_generate_reviewed_code_gives_reviewer_static_gate_schema_and_knowledge() -> None:
    initial = (
        "def run(ctx):\n"
        "    saved = ctx.interact('save order', success=True)\n"
        "    assert saved, 'order must be saved'"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response((
            "success=True",
            "success='order is saved'",
        )),
    ])

    plan = generate_reviewed_code(
        "save the order",
        knowledge="The Orders collection exposes action_url; status is detail-only.",
        fixture=FixtureSpec(
            lookups={"orders": [{"id": "o1", "action_url": "/orders/o1"}]},
            reads={"o1": {"status": "Pending"}},
        ),
        llm=llm,
    )

    assert plan.executable
    assert llm.calls == 2
    review_messages = "\n".join(
        str(getattr(message, "content", ""))
        for message in llm.messages[1]
    )
    generation_messages = "\n".join(
        str(getattr(message, "content", ""))
        for message in llm.messages[0]
    )
    assert "success must be a verifiable string postcondition" in review_messages
    assert "NOT_RUN: static diagnostics must be fixed first" in review_messages
    assert "available_fields=['action_url', 'id']" in review_messages
    assert "available_fields=['status']" in review_messages
    assert "detail-only fields: ['status']" in review_messages
    assert "must be read from each concrete collection record" in review_messages
    assert "status is detail-only" in generation_messages
    assert "status is detail-only" not in review_messages
    assert "Treat that postcondition as idempotent" in generation_messages
    assert "The postcondition is idempotent" in review_messages
    assert "cannot be suppressed with `# noqa`" in review_messages
    assert "qualifiers as selection predicates" in review_messages
    assert "Put every user-requested new or changed literal in `required_values`" in review_messages
    assert "Mandatory final repair gate" in review_messages
    assert review_messages.count(
        "ctx.interact success must be a verifiable string postcondition"
    ) == 2


def test_generate_reviewed_code_applies_one_local_repair() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('orders')\n"
        "    rows = ctx.acquire(scope, fields=['status'])\n"
        "    assert rows, 'an order must exist'\n"
        "    return rows[0]['id']"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response(
            ("fields=['status']", "fields=['status', 'id']"),
            ("fields=['status']", "fields=['status', 'id']"),
        ),
    ])

    plan = generate_reviewed_code(
        "return the order id",
        fixture=FixtureSpec(lookups={"orders": [{"id": "o1", "status": "Pending"}]}),
        llm=llm,
    )

    assert plan.executable
    assert "fields=['status', 'id']" in plan.source
    assert len(plan.attempts) == 2
    assert llm.calls == 2
    assert plan.attempts[0].diagnostics[0].code == "PROJECTED_FIELD_UNAVAILABLE"
    assert plan.attempts[1].run.return_value == "o1"


def test_generate_reviewed_code_applies_structured_json_local_repair() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('orders')\n"
        "    rows = ctx.acquire(scope, fields=['status'])\n"
        "    assert rows, 'an order must exist'\n"
        "    return rows[0]['id']"
    )
    repair = json.dumps({
        "approve": False,
        "edits": [{
            "search": "fields=['status']",
            "replacement": "fields=['status', 'id']",
        }],
    })
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        repair,
    ])

    plan = generate_reviewed_code(
        "return the order id",
        fixture=FixtureSpec(
            lookups={"orders": [{"id": "o1", "status": "Pending"}]},
        ),
        llm=llm,
    )

    assert plan.executable
    assert "fields=['status', 'id']" in plan.source
    assert plan.attempts[1].run.return_value == "o1"
    assert plan.review is not None
    assert plan.review.edits == ((
        "fields=['status']",
        "fields=['status', 'id']",
    ),)


def test_generate_reviewed_code_allows_header_only_import_repair() -> None:
    initial = (
        "from __future__ import annotations\n\n"
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'rows must exist'\n"
        "    return rows"
    )
    repair = json.dumps({
        "approve": False,
        "edits": [{
            "search": "from __future__ import annotations\n\ndef run(ctx):",
            "replacement": "def run(ctx):",
        }],
    })
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        repair,
    ])

    plan = generate_reviewed_code(
        "return the rows",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert plan.source.startswith("def run(ctx):")


def test_generate_reviewed_code_validates_complete_function_replacement() -> None:
    initial = (
        "def run(ctx):\n"
        "    rows = []\n"
        "    assert rows, 'rows must exist'\n"
        "    return rows"
    )
    replacement = (
        "def run(ctx):\n"
        "    value = 42\n"
        "    assert value > 0, 'value must be positive'\n"
        "    return value"
    )
    repair = json.dumps({
        "approve": False,
        "edits": [{
            "search": initial,
            "replacement": replacement,
        }],
    })
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        repair,
    ])

    plan = generate_reviewed_code(
        "return nonempty rows",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert plan.repaired
    assert plan.requirements_satisfied
    assert plan.source == replacement
    assert plan.attempts[-1].run.return_value == 42



def test_generate_reviewed_code_atomically_applies_more_than_five_local_edits() -> None:
    assignments = "\n".join(
        f"    value_{index} = 'old_{index}'"
        for index in range(6)
    )
    initial = (
        "def run(ctx):\n"
        f"{assignments}\n"
        "    values = [value_0, value_1, value_2, value_3, value_4, value_5]\n"
        "    assert values, 'values must exist'\n"
        "    return values"
    )
    repair = json.dumps({
        "approve": False,
        "edits": [
            {
                "search": f"value_{index} = 'old_{index}'",
                "replacement": f"value_{index} = 'new_{index}'",
            }
            for index in range(6)
        ],
    })
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        repair,
    ])

    plan = generate_reviewed_code(
        "return the corrected values",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert plan.attempts[1].run.return_value == [
        "new_0", "new_1", "new_2", "new_3", "new_4", "new_5",
    ]


def test_generate_reviewed_code_keeps_only_edits_that_improve_the_candidate() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('orders')\n"
        "    rows = ctx.acquire(scope, fields=['id', 'status'])\n"
        "    assert rows, 'an order must exist'\n"
        "    saved = ctx.interact(\n"
        "        'notify customer', inputs={'record': rows[0]},\n"
        "        required_values={'message': 'ready'}, persistence='explicit_commit')\n"
        "    assert saved, 'notification must be sent'\n"
        "    return rows[0]['id']"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response(
            (
                "fields=['id', 'status']",
                "fields=['status']",
            ),
            (
                "'notify customer', inputs={'record': rows[0]},",
                "'notify customer', success='notification is sent', "
                "inputs={'record': rows[0]},",
            ),
        ),
    ])

    plan = generate_reviewed_code(
        "notify the customer and return the order id",
        fixture=FixtureSpec(
            lookups={"orders": [{"id": "o1", "status": "Pending"}]},
        ),
        llm=llm,
    )

    assert plan.executable
    assert "fields=['id', 'status']" in plan.source
    assert "success='notification is sent'" in plan.source
    assert plan.attempts[1].run.return_value == "o1"
    assert llm.calls == 2


def test_generate_reviewed_code_keeps_an_executable_candidate_over_a_failed_repair() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('orders')\n"
        "    rows = ctx.acquire(scope, fields=['id'])\n"
        "    assert rows, 'an order must exist'\n"
        "    return rows[0]['id']"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response(
            (
                "fields=['id']",
                "fields=['id', 'missing']",
            ),
        ),
    ])

    plan = generate_reviewed_code(
        "return the order id",
        fixture=FixtureSpec(lookups={"orders": [{"id": "o1"}]}),
        llm=llm,
    )

    assert plan.executable
    assert plan.source == initial
    assert len(plan.attempts) == 1


def test_generate_reviewed_code_keeps_valid_semantic_edit_on_executable_source() -> None:
    initial = (
        "def run(ctx):\n"
        "    scope = ctx.lookup('records')\n"
        "    rows = ctx.acquire(scope, fields=['name', 'uses'])\n"
        "    assert len(rows) > 0, 'records must exist'\n"
        "    ranked = sorted(rows, key=lambda row: row['uses'], reverse=True)\n"
        "    return [row['name'] for row in ranked[:2]]"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        _review_response((
            "assert len(rows) > 0, 'records must exist'",
            "assert len(rows) >= 2, 'two records must exist'",
        )),
    ])

    plan = generate_reviewed_code(
        "return the top two records",
        fixture=FixtureSpec(lookups={
            "records": [
                {"name": "A", "uses": 2},
                {"name": "B", "uses": 1},
            ],
        }),
        llm=llm,
    )

    assert plan.requirements_satisfied
    assert plan.repaired
    assert "len(rows) >= 2" in plan.source
