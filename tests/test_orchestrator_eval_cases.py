from __future__ import annotations

import importlib.util
import json
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


def test_eval_covers_hard_single_site_shopping_tasks() -> None:
    cases = orchestrator_eval.load_cases()
    by_group = {
        group: {case["task_id"] for case in cases if case["group"] == group}
        for group in {"hard_shopping_admin", "hard_shopping"}
    }
    curated = {case["task_id"] for case in cases if case.get("curated")}

    assert len(by_group["hard_shopping_admin"]) == 55
    assert len(by_group["hard_shopping"]) == 56
    assert len(cases) == 111
    assert curated == {
        42, 63, 108, 113, 185, 193, 488, 491, 499, 544, 549, 694, 701, 709,
    }
    assert curated <= by_group["hard_shopping_admin"]
    assert all(case.get("site") in {"shopping_admin", "shopping"} for case in cases)

    origins = {case.get("contract_origin") for case in cases}
    assert origins <= {"curated", "baseline_inferred", "empty"}
    assert sum(1 for case in cases if case.get("contract_origin") == "curated") == 14
    assert sum(1 for case in cases if case.get("contract_origin") == "baseline_inferred") == 97

    for case in cases:
        assert case.get("contract") is not None
        if case.get("curated"):
            assert case.get("contract")
            assert case.get("contract_origin") == "curated"
        baseline = case.get("baseline") or {}
        assert baseline.get("run") == "20260805_hard_single_site_merged"
        assert baseline.get("grade") in {
            "executable_pass",
            "executable_fail",
            "contract_pass",
            "contract_fail",
        }
        if case.get("curated"):
            assert baseline["ok"] == (baseline["grade"] == "contract_pass")
        else:
            assert baseline["ok"] == (baseline["grade"] == "executable_pass")
            assert baseline["ok"] == baseline.get("executable")


def test_grade_sample_scores_only_executable_and_curated() -> None:
    non = {"curated": False, "contract": {"method_counts": {"commit": 0}}}
    curated = {"curated": True, "contract": {"method_counts": {"commit": 0}}}
    # non-curated: contract content ignored for scoring
    assert (
        orchestrator_eval.grade_sample(non, {"ok": True, "executable": True})
        == "executable_pass"
    )
    assert (
        orchestrator_eval.grade_sample(non, {"ok": False, "executable": True})
        == "executable_pass"
    )
    assert (
        orchestrator_eval.grade_sample(non, {"ok": False, "executable": False})
        == "executable_fail"
    )
    assert (
        orchestrator_eval.grade_sample(curated, {"ok": True, "executable": True})
        == "contract_pass"
    )
    assert (
        orchestrator_eval.grade_sample(curated, {"ok": False, "executable": True})
        == "contract_fail"
    )
    assert (
        orchestrator_eval.grade_sample(curated, {"ok": False, "executable": False})
        == "executable_fail"
    )


def test_baseline_index_matches_case_annotations() -> None:
    cases = orchestrator_eval.load_cases()
    index = json.loads(
        (
            PROJECT_ROOT
            / "evals/browser/orchestrator/baseline_qwen37_tokenplan_20260805.json"
        ).read_text(encoding="utf-8")
    )
    assert index["totals"]["cases"] == 111
    assert index["totals"]["ok"] == 89
    assert index["totals"]["executable"] == 91
    assert index["totals"]["curated"] == {"cases": 14, "ok": 12}
    assert index["totals"]["by_grade"] == {
        "executable_pass": 77,
        "executable_fail": 20,
        "contract_pass": 12,
        "contract_fail": 2,
    }
    assert index["totals"]["by_contract_origin"]["baseline_inferred"] == 97
    assert index["totals"]["by_contract_origin"]["curated"] == 14
    assert index["run"]["scoring"]["non_curated_contract"]
    for case in cases:
        entry = index["tasks"][str(case["task_id"])]
        baseline = case["baseline"]
        assert entry["grade"] == baseline["grade"]
        assert entry["ok"] == baseline["ok"]
        assert entry["executable"] == baseline["executable"]
        assert entry["contract_origin"] == case.get("contract_origin")


def test_inferred_contract_accepts_its_baseline_source() -> None:
    """Non-curated inferred contracts are frozen shapes of the baseline program."""
    report = json.loads(
        (
            PROJECT_ROOT
            / "logs/orchestrator_eval/20260805_hard_single_site_merged/report.json"
        ).read_text(encoding="utf-8")
    )
    source_by_id = {
        case["task_id"]: (case.get("samples") or [{}])[0].get("source") or ""
        for case in report["cases"]
    }
    checked = 0
    for case in orchestrator_eval.load_cases():
        if case.get("curated") or case.get("contract_origin") != "baseline_inferred":
            continue
        if not case["baseline"].get("executable"):
            continue
        source = source_by_id[case["task_id"]]
        assert source
        assert orchestrator_eval.evaluate_source(source, case["contract"]) == []
        checked += 1
    assert checked >= 70


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
    assert any("DIRECT_COMMIT_REQUIRED" in item for item in failures)


def test_contract_accepts_required_literal_inside_instruction_text() -> None:
    source = '''
def run(ctx):
    ctx.reach(
        "Search for Beijing highest temperature today",
        success={"entity": "SearchResult", "fields": ["temperature"]},
    )
    value = ctx.read(fields={"temperature": "number"})
    return int(value["temperature"])
'''

    assert orchestrator_eval.evaluate_source(
        source,
        {
            "method_counts": {"commit": 0},
            "literal_substrings": ["today"],
        },
    ) == []


def test_ordered_call_alternatives_accept_only_causally_restored_target_source() -> None:
    contract = {
        "ordered_call_alternatives": [
            [
                {"method": "query", "entity": "Saved"},
                {"method": "query", "entity": "Targets"},
                {"method": "commit", "target_mode": "present"},
            ],
            [
                {"method": "query", "entity": "Targets"},
                {"method": "query", "entity": "Saved"},
                {"method": "reach", "success_include": {"entity": "Targets"}},
                {"method": "commit", "target_mode": "present"},
            ],
        ],
    }
    saved_first = '''
def run(ctx):
    ctx.reach("Saved", success={"entity": "Saved"})
    saved = ctx.query(entity="Saved", fields=["id"])
    ctx.reach("Targets", success={"entity": "Targets"})
    rows = ctx.query(entity="Targets", fields=["id"])
    ctx.reach("Open target", target=rows[0], success={"entity": "Target", "id": rows[0]["id"]})
    ctx.commit("Change target", target=rows[0], values={"enabled": True})
'''
    target_first_with_restore = '''
def run(ctx):
    ctx.reach("Targets", success={"entity": "Targets"})
    rows = ctx.query(entity="Targets", fields=["id"])
    ctx.reach("Saved", success={"entity": "Saved"})
    saved = ctx.query(entity="Saved", fields=["id"])
    ctx.reach("Restore targets", success={"entity": "Targets"})
    ctx.reach("Open target", target=rows[0], success={"entity": "Target", "id": rows[0]["id"]})
    ctx.commit("Change target", target=rows[0], values={"enabled": True})
'''
    target_first_without_restore = target_first_with_restore.replace(
        '    ctx.reach("Restore targets", success={"entity": "Targets"})\n',
        "",
    )

    assert orchestrator_eval.evaluate_source(saved_first, contract) == []
    assert orchestrator_eval.evaluate_source(target_first_with_restore, contract) == []
    assert orchestrator_eval.evaluate_source(target_first_without_restore, contract) == [
        "ORDERED_CALL_ALTERNATIVES"
    ]


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
