from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVAL_FILE = (
    PROJECT_ROOT / "evals/browser/orchestrator/test_orchestrator.py"
)
SPEC = importlib.util.spec_from_file_location("orchestrator_eval", EVAL_FILE)
assert SPEC is not None and SPEC.loader is not None
orchestrator_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(orchestrator_eval)


def _case(task_id: int) -> dict[str, Any]:
    return next(
        case for case in orchestrator_eval.load_cases()
        if case["task_id"] == task_id
    )


def test_eval_groups_cover_query_and_form_regression_tasks() -> None:
    cases = orchestrator_eval.load_cases()
    by_group = {
        group: {case["task_id"] for case in cases if case["group"] == group}
        for group in {"query_20260726", "form_submission"}
    }

    assert by_group == {
        "query_20260726": {42, 63, 108, 113, 185, 193},
        "form_submission": {488, 491, 499, 544, 549, 694, 701, 709},
    }


def test_query_contract_accepts_typed_monthly_aggregation_plan() -> None:
    source = '''
from datetime import datetime

def run(ctx):
    start_date = "01/01/2023"
    end_date = "05/31/2023"
    ctx.reach(
        "Go to Sales > Orders",
        success={"entity": "Orders"},
    )
    rows = ctx.query(entity="Orders",
        fields={"Purchase Date": "datetime"},
        filters={
            "Status": "Complete",
            "Purchase Date": {"from": start_date, "to": end_date},
        },
    )
    names = ["January", "February", "March", "April", "May"]
    counts = {name: 0 for name in names}
    for row in rows:
        value = row["Purchase Date"]
        date = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        counts[date.strftime("%B")] += 1
    return [{"month": name, "count": counts[name]} for name in names]
'''

    assert (
        orchestrator_eval.evaluate_source(source, _case(108)["contract"])
        == []
    )


def test_form_contract_accepts_attribute_then_product_fallback_plan() -> None:
    source = '''
def run(ctx):
    ctx.reach(
        "Go to Product Attributes",
        success={"entity": "Product Attributes"},
    )
    attributes = ctx.query(entity="Product Attributes",
        fields=["Attribute Code"],
        filters={"Default Label": "Size"},
    )
    assert attributes, "Size attribute was not found"
    ctx.reach(
        "Open the exact attribute",
        target=attributes[0],
        success={
            "entity": "Product Attribute",
            "Attribute Code": attributes[0]["Attribute Code"],
        },
    )
    ctx.commit(
        "Add the XXXL size option",
        target=attributes[0],
        values={"Admin Description": "XXXL", "Admin Swatch": "XXXL"},
    )
    ctx.reach(
        "Go to Products",
        success={"entity": "Products"},
    )
    products = ctx.query(entity="Products",
        fields=["Name", "Type"],
        filters={"Name": "Minerva LumaTech V-Tee"},
    )
    if not products:
        products = ctx.query(entity="Products",
            fields=["Name", "Type"],
            filters={"Name": "Minerva"},
        )
    owners = [
        product
        for product in products
        if product["Type"] == "Configurable Product"
    ]
    assert len(owners) == 1, "Expected one configurable Minerva owner"
    ctx.reach(
        "Open the exact product",
        target=owners[0],
        success={
            "entity": "Product",
            "Name": owners[0]["Name"],
            "Type": owners[0]["Type"],
        },
    )
    ctx.commit(
        "Add the green XXXL configuration",
        target=owners[0],
        values={"Configurations": [{"Color": "green", "Size": "XXXL"}]},
    )
'''

    assert (
        orchestrator_eval.evaluate_source(source, _case(549)["contract"])
        == []
    )


def test_form_contract_rejects_extra_navigation_for_direct_creation() -> None:
    source = '''
def run(ctx):
    ctx.reach(
        "Go to Catalog > Products",
        success={"entity": "Products"},
    )
    ctx.commit(
        "Create the simple product",
        target=None,
        values={
            "Name": "Energy-Bulk Women Shirt",
            "Price": 60,
            "Quantity": 50,
            "Stock Status": "In Stock",
            "Size": "S",
            "Color": "blue",
        },
    )
'''

    failures = orchestrator_eval.evaluate_source(
        source,
        _case(694)["contract"],
    )

    assert failures
    assert any("METHOD_COUNT:reach" in item for item in failures)


def test_report_contract_accepts_runtime_formatted_dates() -> None:
    source = '''
from datetime import date

def run(ctx):
    start = date(2021, 5, 1).strftime("%m/%d/%Y")
    end = date(2022, 3, 31).strftime("%m/%d/%Y")
    ctx.reach(
        "Show Orders report",
        success={
            "entity": "Sales Reports",
            "Report Type": "Orders",
            "From": start,
            "To": end,
            "rendered": True,
        },
    )
'''

    assert (
        orchestrator_eval.evaluate_source(source, _case(709)["contract"])
        == []
    )


def test_ranked_contract_accepts_max_with_typed_key() -> None:
    source = '''
def run(ctx):
    ctx.reach("View Orders", success={"entity": "Orders"})
    rows = ctx.query(entity="Orders",
        fields={"Purchase Date": "datetime"},
        filters={"Bill-to Name": "Sarah Miller", "Status": "Pending"},
    )
    if not rows:
        rows = ctx.query(entity="Orders",
            fields={"Purchase Date": "datetime"},
            filters={"Bill-to Name": "Sarah", "Status": "Pending"},
        )
    assert rows, "No pending order found"
    latest = max(rows, key=lambda row: row["Purchase Date"])
    ctx.reach(
        "Open the exact order",
        target=latest,
        success={
            "entity": "Order",
            "Purchase Date": latest["Purchase Date"],
        },
    )
    ctx.commit(
        "Notify customer",
        target=latest,
        values={
            "Comment": "the order is ready to be shipped soon!",
            "Notify Customer by Email": True,
        },
    )
'''

    assert (
        orchestrator_eval.evaluate_source(source, _case(491)["contract"])
        == []
    )
