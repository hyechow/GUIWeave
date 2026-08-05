from __future__ import annotations

import ast
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


def _is_ctx(node: ast.AST, method: str | None = None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
        and (method is None or node.func.attr == method)
    )


def _insert_state(call: ast.Call) -> None:
    call.args.insert(0, ast.Name(id="state", ctx=ast.Load()))


class _OldSourceMigrator(ast.NodeTransformer):
    """Mechanically migrate old-style ctx programs to the explicit-state API.

    The historical baselines in ``logs/orchestrator_eval/.../report.json`` were
    generated before ``ctx`` grew the explicit ``state`` argument and the
    ``query``/``acquire`` split. This transform re-expresses them so the
    migrated programs exercise the same contract the live planner now emits:
    ``def run(ctx, state)``, every reach/commit/command returns and threads the
    next state, and every query is split into ``ctx.query(state, ...)``
    (scope) + ``ctx.acquire(scope, fields=...)``.
    """

    def __init__(self, function: ast.FunctionDef) -> None:
        self.function = function
        self.used_names = {
            node.id for node in ast.walk(function) if isinstance(node, ast.Name)
        }
        self.scope_seq = 0

    def _fresh_scope(self) -> str:
        while True:
            self.scope_seq += 1
            name = f"_scope{self.scope_seq}"
            if name not in self.used_names:
                self.used_names.add(name)
                return name

    def _split_query(self, assign: ast.Assign, call: ast.Call) -> list[ast.AST]:
        target = assign.targets[0]
        fields_node = None
        coverage_node = None
        kept: list[ast.keyword] = []
        for kw in call.keywords:
            if kw.arg == "fields":
                fields_node = kw.value
            elif kw.arg == "coverage":
                coverage_node = kw.value
            else:
                kept.append(kw)
        call.keywords = kept
        _insert_state(call)
        scope_name = self._fresh_scope()
        scope_assign = ast.Assign(
            targets=[ast.Name(id=scope_name, ctx=ast.Store())], value=call
        )
        ast.copy_location(scope_assign, assign)
        acquire_kwargs = [ast.keyword(arg="fields", value=fields_node)]
        if coverage_node is not None:
            acquire_kwargs.append(ast.keyword(arg="coverage", value=coverage_node))
        acquire_call = ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="ctx", ctx=ast.Load()), attr="acquire"
            ),
            args=[ast.Name(id=scope_name, ctx=ast.Load())],
            keywords=acquire_kwargs,
        )
        ast.copy_location(acquire_call, call)
        rows_assign = ast.Assign(targets=[target], value=acquire_call)
        ast.copy_location(rows_assign, assign)
        return [scope_assign, rows_assign]

    def visit_Assign(self, node: ast.Assign) -> Any:  # noqa: N802
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _is_ctx(node.value, "query")
        ):
            return self._split_query(node, node.value)
        self.generic_visit(node)
        return node

    def visit_Expr(self, node: ast.Expr) -> Any:  # noqa: N802
        if (
            isinstance(node.value, ast.Call)
            and _is_ctx(node.value)
            and node.value.func.attr in {"reach", "commit", "command"}
        ):
            call = node.value
            _insert_state(call)
            self.generic_visit(call)
            assign = ast.Assign(
                targets=[ast.Name(id="state", ctx=ast.Store())], value=call
            )
            ast.copy_location(assign, node)
            return assign
        self.generic_visit(node)
        return node

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        if _is_ctx(node) and node.func.attr in {"read", "command", "query"}:
            _insert_state(node)
        elif _is_ctx(node) and node.func.attr in {"reach", "commit"}:
            _insert_state(node)
        self.generic_visit(node)
        return node


def _migrate_old_source(source: str) -> str:
    """Convert one legacy ``def run(ctx)`` program to the explicit-state API."""
    tree = ast.parse(source)
    new_body: list[ast.AST] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "run":
            stmt.args.args.append(ast.arg(arg="state", annotation=None))
            new_body.append(_OldSourceMigrator(stmt).visit(stmt))
        else:
            new_body.append(stmt)
    tree.body = new_body
    ast.fix_missing_locations(tree)
    return ast.unparse(tree).strip()


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
    """Baseline annotations embedded in cases.json satisfy the frozen index bars."""
    cases = orchestrator_eval.load_cases()
    by_grade: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    curated = {"cases": 0, "ok": 0}
    ok = 0
    executable = 0
    for case in cases:
        baseline = case["baseline"]
        grade = baseline["grade"]
        by_grade[grade] = by_grade.get(grade, 0) + 1
        origin = str(case.get("contract_origin") or "empty")
        by_origin[origin] = by_origin.get(origin, 0) + 1
        if baseline["ok"]:
            ok += 1
        if baseline["executable"]:
            executable += 1
        if case.get("curated"):
            curated["cases"] += 1
            if baseline["ok"]:
                curated["ok"] += 1

    assert len(cases) == 111
    assert ok == 89
    assert executable == 91
    assert curated == {"cases": 14, "ok": 12}
    assert by_grade == {
        "executable_pass": 77,
        "executable_fail": 20,
        "contract_pass": 12,
        "contract_fail": 2,
    }
    assert by_origin["baseline_inferred"] == 97
    assert by_origin["curated"] == 14
    for case in cases:
        baseline = case["baseline"]
        assert baseline.get("run") == "20260805_hard_single_site_merged"
        assert baseline["grade"] in {
            "executable_pass",
            "executable_fail",
            "contract_pass",
            "contract_fail",
        }
        assert baseline["ok"] == (baseline["grade"] in {"executable_pass", "contract_pass"})
        if case.get("curated"):
            assert baseline["ok"] == (baseline["grade"] == "contract_pass")
        else:
            assert baseline["ok"] == (baseline["grade"] == "executable_pass")
            assert baseline["ok"] == baseline["executable"]


def test_inferred_contract_accepts_its_baseline_source() -> None:
    """Non-curated inferred contracts are frozen shapes of the baseline program.

    The recorded baselines predate the explicit-state ctx API, so each legacy
    source is mechanically migrated before it is checked against its contract.
    """
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
        source = _migrate_old_source(source_by_id[case["task_id"]])
        assert source
        assert orchestrator_eval.evaluate_source(source, case["contract"]) == []
        checked += 1
    assert checked >= 70


def test_query_contract_accepts_typed_monthly_aggregation_plan() -> None:
    source = '''
from datetime import datetime

def run(ctx, state):
    start_date = "01/01/2023"
    end_date = "05/31/2023"
    state = ctx.reach(
        state,
        "Go to Sales > Orders",
        success={"entity": "Orders"},
    )
    scope = ctx.query(state, entity="Orders",
        filters={
            "Status": "Complete",
            "Purchase Date": {"from": start_date, "to": end_date},
        },
    )
    rows = ctx.acquire(scope, fields={"Purchase Date": "datetime"})
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
def run(ctx, state):
    state = ctx.reach(
        state,
        "Go to Product Attributes",
        success={"entity": "Product Attributes"},
    )
    attr_scope = ctx.query(state, entity="Product Attributes",
        filters={"Default Label": "Size"},
    )
    attributes = ctx.acquire(attr_scope, fields=["Attribute Code"])
    assert attributes, "Size attribute was not found"
    state = ctx.reach(
        state,
        "Open the exact attribute",
        target=attributes[0],
        success={
            "entity": "Product Attribute",
            "Attribute Code": attributes[0]["Attribute Code"],
        },
    )
    state = ctx.commit(
        state,
        "Add the XXXL size option",
        target=attributes[0],
        values={"Admin Description": "XXXL", "Admin Swatch": "XXXL"},
    )
    state = ctx.reach(
        state,
        "Go to Products",
        success={"entity": "Products"},
    )
    prod_scope = ctx.query(state, entity="Products",
        filters={"Name": "Minerva LumaTech V-Tee"},
    )
    products = ctx.acquire(prod_scope, fields=["Name", "Type"])
    if not products:
        prod_scope = ctx.query(state, entity="Products",
            filters={"Name": "Minerva"},
        )
        products = ctx.acquire(prod_scope, fields=["Name", "Type"])
    owners = [
        product
        for product in products
        if product["Type"] == "Configurable Product"
    ]
    assert len(owners) == 1, "Expected one configurable Minerva owner"
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
    state = ctx.commit(
        state,
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
def run(ctx, state):
    state = ctx.reach(
        state,
        "Go to Catalog > Products",
        success={"entity": "Products"},
    )
    state = ctx.commit(
        state,
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
def run(ctx, state):
    state = ctx.reach(
        state,
        "Search for Beijing highest temperature today",
        success={"entity": "SearchResult", "fields": ["temperature"]},
    )
    value = ctx.read(state, fields={"temperature": "number"})
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
def run(ctx, state):
    state = ctx.reach(state, "Saved", success={"entity": "Saved"})
    saved_scope = ctx.query(state, entity="Saved")
    saved = ctx.acquire(saved_scope, fields=["id"])
    state = ctx.reach(state, "Targets", success={"entity": "Targets"})
    target_scope = ctx.query(state, entity="Targets")
    rows = ctx.acquire(target_scope, fields=["id"])
    state = ctx.reach(state, "Open target", target=rows[0], success={"entity": "Target", "id": rows[0]["id"]})
    state = ctx.commit(state, "Change target", target=rows[0], values={"enabled": True})
'''
    target_first_with_restore = '''
def run(ctx, state):
    state = ctx.reach(state, "Targets", success={"entity": "Targets"})
    target_scope = ctx.query(state, entity="Targets")
    rows = ctx.acquire(target_scope, fields=["id"])
    state = ctx.reach(state, "Saved", success={"entity": "Saved"})
    saved_scope = ctx.query(state, entity="Saved")
    saved = ctx.acquire(saved_scope, fields=["id"])
    state = ctx.reach(state, "Restore targets", success={"entity": "Targets"})
    state = ctx.reach(state, "Open target", target=rows[0], success={"entity": "Target", "id": rows[0]["id"]})
    state = ctx.commit(state, "Change target", target=rows[0], values={"enabled": True})
'''
    target_first_without_restore = target_first_with_restore.replace(
        '    state = ctx.reach(state, "Restore targets", success={"entity": "Targets"})\n',
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

def run(ctx, state):
    start = date(2021, 5, 1).strftime("%m/%d/%Y")
    end = date(2022, 3, 31).strftime("%m/%d/%Y")
    state = ctx.reach(
        state,
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
def run(ctx, state):
    state = ctx.reach(state, "View Orders", success={"entity": "Orders"})
    order_scope = ctx.query(state, entity="Orders",
        filters={"Bill-to Name": "Sarah Miller", "Status": "Pending"},
    )
    rows = ctx.acquire(order_scope, fields={"Purchase Date": "datetime"})
    if not rows:
        order_scope = ctx.query(state, entity="Orders",
            filters={"Bill-to Name": "Sarah", "Status": "Pending"},
        )
        rows = ctx.acquire(order_scope, fields={"Purchase Date": "datetime"})
    assert rows, "No pending order found"
    latest = max(rows, key=lambda row: row["Purchase Date"])
    state = ctx.reach(
        state,
        "Open the exact order",
        target=latest,
        success={
            "entity": "Order",
            "Purchase Date": latest["Purchase Date"],
        },
    )
    state = ctx.commit(
        state,
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


def test_eval_ctx_positions_match_sandbox_signatures() -> None:
    """The eval checker's ctx position table must mirror the sandbox's signature.

    The sandbox's CTX_SIGNATURES is authoritative for the ctx API; if the eval's
    CTX_POSITIONS drifts (e.g. after a signature change), the eval would grade
    against a stale shape. Keep them consistent by construction.
    """
    from gui_agent.core.orchestrator.sandbox import CTX_SIGNATURES

    for method, (positional, _required) in CTX_SIGNATURES.items():
        expected = {name: index for index, name in enumerate(positional)}
        assert orchestrator_eval.CTX_POSITIONS.get(method) == expected, (
            f"eval CTX_POSITIONS[{method!r}] drifted from sandbox CTX_SIGNATURES: "
            f"expected {expected}, got {orchestrator_eval.CTX_POSITIONS.get(method)}"
        )
