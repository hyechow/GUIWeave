from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.core.coding_orchestrator import (
    FixtureSpec,
    execute_code,
    generate_code,
    generate_reviewed_code,
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


class _SequenceLlm:
    def __init__(self, sources: list[str]) -> None:
        self.sources = iter(sources)
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(
            content=f"```python\n{next(self.sources)}\n```",
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
        )


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
    repair = (
        "<<<<<<< SEARCH\n"
        "target = {'id': 'missing'}\n"
        "=======\n"
        "target = {'id': 'record-1'}\n"
        ">>>>>>> REPLACE"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        repair,
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
    assert len(plan.reviews) == 1
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
        "APPROVE",
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


def test_generate_reviewed_code_accepts_approve_as_the_review_conclusion() -> None:
    source = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'fixture task must have work'\n"
        "    return 'done'"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{source}\n```",
        "The candidate satisfies the task and mock contract.\n\nAPPROVE",
    ])

    plan = generate_reviewed_code(
        "complete a fixture task",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert not plan.repaired
    assert plan.review is not None and plan.review.approved
    assert llm.calls == 2


def test_generate_reviewed_code_bounds_the_reviewer_output() -> None:
    source = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    assert rows, 'fixture task must have work'\n"
        "    return 'done'"
    )
    llm = _BindableContentSequenceLlm([
        f"```python\n{source}\n```",
        "APPROVE",
    ])

    plan = generate_reviewed_code(
        "complete a fixture task",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert llm.bind_calls == [{
        "max_tokens": 3072,
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
        "APPROVE",
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


def test_generate_reviewed_code_gives_reviewer_static_gate_schema_and_knowledge() -> None:
    initial = (
        "def run(ctx):\n"
        "    saved = ctx.interact('save order', success=True)\n"
        "    assert saved, 'order must be saved'"
    )
    repair = (
        "<<<<<<< SEARCH\n"
        "success=True\n"
        "=======\n"
        "success='order is saved'\n"
        ">>>>>>> REPLACE"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        repair,
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
    repair = (
        "<<<<<<< SEARCH\n"
        "fields=['status']\n"
        "=======\n"
        "fields=['status', 'id']\n"
        ">>>>>>> REPLACE"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        f"{repair}\n\n{repair}",
    ])

    plan = generate_reviewed_code(
        "return the order id",
        fixture=FixtureSpec(lookups={"orders": [{"id": "o1", "status": "Pending"}]}),
        llm=llm,
    )

    assert plan.executable
    assert "fields=['status', 'id']" in plan.source
    assert len(plan.attempts) == 2
    assert len(plan.reviews) == 1
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
    assert plan.review.text.startswith("<<<<<<< SEARCH")


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


def test_generate_reviewed_code_rejects_complete_function_replacement() -> None:
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

    assert not plan.executable
    assert any(
        diagnostic.code == "LOCAL_REPAIR_INVALID"
        and "complete run function" in diagnostic.message
        for diagnostic in plan.attempts[-1].diagnostics
    )


def test_generate_reviewed_code_minimizes_a_local_change_wrapped_as_a_function() -> None:
    initial = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    count = len(rows)\n"
        "    return count"
    )
    replacement = (
        "def run(ctx):\n"
        "    rows = [1]\n"
        "    count = len(rows)\n"
        "    assert count >= 0, 'count must be nonnegative'\n"
        "    return count"
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
        "return the count",
        fixture=FixtureSpec(),
        llm=llm,
    )

    assert plan.executable
    assert "assert count >= 0" in plan.source


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


def test_generate_reviewed_code_discards_a_local_edit_that_breaks_the_repair() -> None:
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
    harmful = (
        "<<<<<<< SEARCH\n"
        "fields=['id', 'status']\n"
        "=======\n"
        "fields=['status']\n"
        ">>>>>>> REPLACE"
    )
    required = (
        "<<<<<<< SEARCH\n"
        "'notify customer', inputs={'record': rows[0]},\n"
        "=======\n"
        "'notify customer', success='notification is sent', "
        "inputs={'record': rows[0]},\n"
        ">>>>>>> REPLACE"
    )
    llm = _ContentSequenceLlm([
        f"```python\n{initial}\n```",
        (
            f"{harmful}\n\n"
            "<<<<<<< SEARCH\n"
            "exact text copied from the candidate\n"
            "=======\n"
            "replacement text\n"
            ">>>>>>> REPLACE\n\n"
            f"{required}"
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


def test_generate_code_repairs_static_or_execution_failure_once() -> None:
    llm = _SequenceLlm([
        "def run(ctx):\n    target = {'id': 'missing'}\n    assert target, 'target must exist'\n    ctx.read(target, fields=['price'])",
        "def run(ctx):\n    rows = [1]\n    assert rows, 'fixture task must have work'\n    return 'done'",
    ])

    plan = generate_code("complete a fixture task", fixture=_fixture(), llm=llm)

    assert plan.executable
    assert plan.repaired
    assert llm.calls == 2
    assert plan.attempts[0].run is not None and not plan.attempts[0].run.ok
    assert plan.attempts[1].run is not None and plan.attempts[1].run.ok


def test_generate_code_does_not_repair_a_successful_execution() -> None:
    llm = _SequenceLlm([
        "def run(ctx):\n    rows = [1]\n    assert rows, 'fixture task must have work'\n    return 'done'",
    ])

    plan = generate_code("complete a fixture task", fixture=_fixture(), llm=llm)

    assert plan.executable
    assert not plan.repaired
    assert llm.calls == 1


def test_generate_code_repairs_failed_business_assertion_once() -> None:
    llm = _SequenceLlm([
        "def run(ctx):\n    rows = []\n    assert rows, 'required targets must exist'",
        "def run(ctx):\n    rows = [1]\n    assert rows, 'required targets must exist'",
    ])

    plan = generate_code("complete a fixture task", fixture=_fixture(), llm=llm)

    assert plan.executable
    assert plan.repaired
    assert plan.attempts[0].run is not None
    assert "required targets must exist" in plan.attempts[0].run.error
