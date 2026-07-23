from __future__ import annotations

from types import SimpleNamespace

import pytest

from gui_agent.core.coding_orchestrator import (
    FixtureSpec,
    execute_code,
    generate_code,
    validate_code,
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
            target=product,
            values={"price": new_price},
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


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("import os\ndef run(ctx):\n    pass", "UNSAFE_IMPORT"),
        ("def run(ctx):\n    while True:\n        pass", "UNSAFE_SYNTAX"),
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
            "def run(ctx):\n    ok = ctx.interact('filter rows', persistence='immediate_commit')\n    assert ok, 'filter applied'",
            "INTERACT_PERSISTENCE",
        ),
        (
            "def run(ctx):\n    rows = ctx.acquire(scope=None, fields=['id'])\n    assert rows, 'rows required'",
            "ACQUIRE_SCOPE_REQUIRED",
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
    assert interaction.kwargs["target"]["id"] == "p1"
    assert interaction.kwargs["values"]["price"] == 86.5
    assert interaction.kwargs["persistence"] == "explicit_commit"


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


def test_fixture_durable_interaction_updates_read_state() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("Sahara leggings")
    products = ctx.acquire(scope, fields=["sku"], coverage="complete")
    assert products, "a product must exist"
    saved = ctx.interact(
        "save product price",
        target=products[0],
        values={"Price": 64.88},
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
