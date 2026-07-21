#!/usr/bin/env python3
"""Focused live eval for the semantic Program compiler.

Run:
  uv run python evals/browser/orchestrator/test_orchestrator_decompose.py
  uv run python evals/browser/orchestrator/test_orchestrator_decompose.py --label foreach

This eval intentionally checks architecture-level invariants rather than a
site recipe. UI routes, controls, SQL and per-row subprograms belong to runtime
executors and must not be baked into the Program.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import (
    Acquire,
    Command,
    Compute,
    Read,
    SourceCheck,
    Finish,
    ForEach,
    If,
    Interact,
    OrchestratorCompileError,
    Program,
    decompose,
    validate_program,
)
from gui_agent.core.orchestrator.program import Stmt
from gui_agent.core.router import EntityRef, IntentResolution
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app


@dataclass(frozen=True)
class Case:
    label: str
    goal: str
    required_text: tuple[str, ...] = ()
    required_compute_text: tuple[str, ...] = ()
    expect_foreach: bool = False
    # Finish-consumed numbers must be defined by Compute (AST/dataflow, not word lists).
    expect_finish_number_from_compute: bool = False
    expect_materialized_data: bool = False
    required_materialized_text: tuple[str, ...] = ()
    resolution: IntentResolution | None = None
    expect_lookup_fallback: bool = False
    knowledge_app: str = ""
    expect_relational_fallback: bool = False


CASES = (
    Case(
        "interact",
        "In the current app, set the selected profile's notification level to High and save it.",
        ("High",),
    ),
    Case(
        "known-url",
        "Open https://example.test/settings and make the Privacy section visible.",
        ("https://example.test/settings", "Privacy"),
    ),
    Case(
        "data-if",
        "Read the current result data, and only if there are matching records, open the first matching record.",
    ),
    Case(
        "foreach",
        "From the currently available record data, select all records marked pending and apply the same archive operation to each selected record.",
        expect_foreach=True,
    ),
    Case(
        "count-after-filter",
        "Get the total number of reviews in the store that mention the term best.",
        ("best",),
        expect_finish_number_from_compute=True,
    ),
    Case(
        "rank-customer",
        "Get customer email(s) who completed the second most number of orders in the entire history.",
        expect_finish_number_from_compute=False,  # answer is a tie-preserving record list
        expect_materialized_data=True,
    ),
    Case(
        "monthly-count-filtered-range",
        "Get the monthly count of completed orders from January 2023 through May 2023, inclusive. "
        'Return a list of objects with keys "month" and "count" only.',
        required_text=("completed", "January 2023", "May 2023"),
        expect_materialized_data=True,
    ),
    Case(
        "data-top-n",
        "From the current result data, return the top 2 labels ranked by usage.",
        required_compute_text=("top 2",),
    ),
    Case(
        "lookup-fallback",
        "Find the product named Aurora Jacket and open its matching record.",
        resolution=IntentResolution(entities=[EntityRef(
            mention="Aurora Jacket",
            role="lookup",
            type="product",
            match_mode="approximate",
            search_key="Aurora",
        )]),
        expect_lookup_fallback=True,
    ),
    Case(
        "product-material-owner-fallback",
        "Give me the material of the products that have 3 units left",
        required_text=("material", "3"),
        expect_foreach=True,
        expect_materialized_data=True,
        knowledge_app="shopping_admin",
        expect_relational_fallback=True,
    ),
)


def _walk(statements: list[Stmt]):
    for statement in statements:
        yield statement
        if isinstance(statement, If):
            yield from _walk(statement.then)
            yield from _walk(statement.otherwise)
        elif isinstance(statement, ForEach):
            yield from _walk(statement.body)


def _bind_origins(statements: list[Stmt]) -> dict[str, str]:
    """Map bind name to its producing operation. Ignores If/ForEach merges."""
    origins: dict[str, str] = {}
    for node in _walk(statements):
        if isinstance(node, (Interact, Acquire, Read, SourceCheck, Compute, Command)) and node.bind:
            origins[node.bind] = node.op
    return origins


def _check_finish_numbers_from_compute(program: Program) -> list[str]:
    """AST invariant: every Finish-cited number field's bind is produced by Compute."""
    errors: list[str] = []
    origins = _bind_origins(program.statements)
    returns_by_bind: dict[str, dict] = {}
    for node in _walk(program.statements):
        if isinstance(node, (Interact, Acquire, Read, SourceCheck, Compute, Command)) and node.bind:
            returns_by_bind[node.bind] = dict(node.returns)

    for node in _walk(program.statements):
        if not isinstance(node, Finish):
            continue
        for out_name, ref in node.outputs.items():
            if not ref.path or not isinstance(ref.path[0], str):
                continue
            field = ref.path[0]
            spec = (returns_by_bind.get(ref.var) or {}).get(field)
            if spec is None or getattr(spec, "type", None) != "number":
                continue
            origin = origins.get(ref.var)
            if origin != "compute":
                errors.append(
                    f"finish.outputs.{out_name} cites number {ref.var}.{field} "
                    f"from op={origin!r}; expected Compute (FINISH_NUMERIC_FROM_COMPUTE)"
                )
    return errors


def _check(case: Case, program: Program) -> list[str]:
    errors = [f"{issue.code}: {issue}" for issue in validate_program(program)]
    nodes = list(_walk(program.statements))
    executors = [
        node for node in nodes
        if isinstance(node, (Interact, Acquire, Read, SourceCheck, Compute, Command))
    ]
    if not executors:
        errors.append("Program contains no executor-backed statement")
    payload = program.model_dump_json().casefold()
    for value in case.required_text:
        if value.casefold() not in payload:
            errors.append(f"required value was dropped: {value!r}")
    compute_payload = " ".join(
        node.model_dump_json() for node in nodes if isinstance(node, Compute)
    ).casefold()
    for value in case.required_compute_text:
        if value.casefold() not in compute_payload:
            errors.append(f"Compute dropped semantic constant: {value!r}")
    if case.expect_foreach and not any(isinstance(node, ForEach) for node in nodes):
        errors.append("expected explicit ForEach over materialized data")
    for loop in (node for node in nodes if isinstance(node, ForEach)):
        if not loop.body:
            errors.append("ForEach body is empty")
        if any(hasattr(loop, field) for field in ("body_goal", "member_desc")):
            errors.append("ForEach exposes retired runtime expansion fields")
    # Always enforce the structural number gate on live programs (same as compile-time).
    errors.extend(_check_finish_numbers_from_compute(program))
    if case.expect_finish_number_from_compute:
        if not any(isinstance(node, Compute) for node in nodes):
            errors.append("count/aggregate goal expected at least one Compute statement")
        if not any(isinstance(node, Interact) for node in nodes):
            errors.append("count-after-filter goal expected an Interact for UI scope")
        has_finish_number = False
        for node in nodes:
            if not isinstance(node, Finish):
                continue
            for ref in node.outputs.values():
                if not ref.path or not isinstance(ref.path[0], str):
                    continue
                bind_returns = {}
                for n in nodes:
                    if isinstance(n, (Interact, Acquire, Read, SourceCheck, Compute, Command)) and n.bind == ref.var:
                        bind_returns = dict(n.returns)
                        break
                spec = bind_returns.get(ref.path[0])
                if spec is not None and getattr(spec, "type", None) == "number":
                    has_finish_number = True
        if not has_finish_number:
            errors.append("expected Finish to cite a number field from Compute")
    if case.label == "rank-customer":
        completed_filter = any(
            any("complete" in str(value).casefold() for value in node.required_values.values())
            for node in nodes
            if isinstance(node, Interact)
        )
        if not completed_filter:
            errors.append(
                "user-specified completed status must be pushed into the UI scope before Acquire"
            )
        compute_nodes = [node for node in nodes if isinstance(node, Compute)]
        if not compute_nodes:
            errors.append("rank/group goal expected a Compute statement for ranking/aggregation")
        rank_outputs = [spec for node in compute_nodes for spec in node.returns.values()]
        if not any(
            spec.type == "list[record]"
            and any("email" in field.casefold() for field in spec.fields)
            for spec in rank_outputs
        ):
            errors.append(
                "rank with possible ties must return list[record] with an email field"
            )
        if not any(
            any("email" in field.casefold() for field in node.required_fields)
            for node in compute_nodes
        ):
            errors.append("rank Compute must require a customer email source field")
        source_fields = {
            field.casefold()
            for node in compute_nodes
            for field in node.required_fields
        }
        if any("count" in field or "rank" in field for field in source_fields):
            errors.append(
                "derived count/rank values must not be declared as source required_fields"
            )
        if any(
            any("count" in field.casefold() or "rank" in field.casefold() for field in spec.fields)
            for spec in rank_outputs
            if spec.type == "list[record]"
        ):
            errors.append("email-only answer must not expose helper count/rank fields")
    if case.expect_materialized_data:
        collection_specs = [
            spec
            for node in nodes
            if isinstance(node, Acquire) and node.bind
            for spec in node.returns.values()
            if spec.type == "list[record]" and spec.coverage == "complete"
        ]
        collection_binds = {
            node.bind
            for node in nodes
            if isinstance(node, Acquire)
            and node.bind
            and any(
                spec.type == "list[record]" and spec.coverage == "complete"
                for spec in node.returns.values()
            )
        }
        compute_inputs = {
            ref.var
            for node in nodes
            if isinstance(node, Compute)
            for ref in node.inputs.values()
        }
        unchecked_acquires = [
            node.id
            for node in nodes
            if isinstance(node, Acquire) and node.source_check is None
        ]
        if not collection_binds:
            errors.append("full-scope aggregation requires complete Acquire collection output")
        elif collection_binds.isdisjoint(compute_inputs):
            errors.append("Compute must consume the materialized complete collection via inputs")
        if unchecked_acquires:
            errors.append(
                "Acquire must consume a SourceCheck availability result: "
                + ", ".join(unchecked_acquires)
            )
        collection_contract = " ".join(spec.description for spec in collection_specs).casefold()
        for value in case.required_materialized_text:
            if value.casefold() not in collection_contract:
                errors.append(
                    f"complete collection contract dropped required field: {value!r}"
                )
        compute_record_specs = [
            spec
            for node in nodes
            if isinstance(node, Compute)
            for spec in node.returns.values()
            if spec.type == "list[record]"
        ]
        if compute_record_specs and any(not spec.fields for spec in compute_record_specs):
            errors.append("Compute list[record] output must declare fields")
    if case.expect_lookup_fallback:
        exact = [
            node for node in nodes
            if isinstance(node, Interact)
            and node.required_values.get("lookup_entity") == "Aurora Jacket"
            and node.required_values.get("query") == "Aurora Jacket"
            and node.required_values.get("match_mode") == "exact"
        ]
        fuzzy = [
            node for node in nodes
            if isinstance(node, Interact)
            and node.required_values.get("lookup_entity") == "Aurora Jacket"
            and node.required_values.get("query") == "Aurora"
            and node.required_values.get("match_mode") == "approximate"
        ]
        zero_branches = [
            node for node in nodes
            if isinstance(node, If) and node.cond.cmp == "==" and node.cond.value == 0
        ]
        existence_branches = [
            node for node in nodes
            if isinstance(node, If) and node.cond.cmp == ">" and node.cond.value == 0
            and node.then and node.otherwise
        ]
        if not exact or not fuzzy or not zero_branches or not existence_branches:
            errors.append(
                "approximate lookup must lower to exact -> count==0 -> fallback -> existence If"
            )
    if case.expect_relational_fallback:
        loops = [node for node in nodes if isinstance(node, ForEach)]
        acquired_fields = {
            field.casefold()
            for node in nodes
            if isinstance(node, Acquire)
            for field in node.required_fields
        }
        if "material" in acquired_fields:
            errors.append("initial Acquire must not require detail-only material")
        if not any("sku" in field for field in acquired_fields):
            errors.append("candidate Acquire must preserve product SKU identity")
        if not loops or not any(loop.collect is not None and loop.into for loop in loops):
            errors.append("related-field fallback requires a collecting ForEach")
        if not any(
            isinstance(node, If) and node.cond.cmp == "empty"
            for loop in loops for node in _walk(loop.body)
        ):
            errors.append("related-field fallback requires an explicit empty-value If")
        split_steps = [
            step
            for node in nodes
            if isinstance(node, Compute)
            for step in node.steps
            if step.op == "text_split"
        ]
        if not any(
            step.separator == "-"
            and step.direction == "right"
            and step.maxsplit == 2
            and step.index == 0
            for step in split_steps
        ):
            errors.append("parent SKU must be derived by Compute text_split('-', right, 2, 0)")
        loop_nodes = [node for loop in loops for node in _walk(loop.body)]
        if not any(
            isinstance(loop.body[index], Command)
            and loop.body[index].capability == "open_url"
            and isinstance(loop.body[index + 1], Interact)
            and loop.body[index + 1].observe_fields == ["Material"]
            and isinstance(loop.body[index + 2], Read)
            for loop in loops
            for index in range(len(loop.body) - 2)
        ):
            errors.append(
                "detail reads must expose the owned field through Interact before Read"
            )
        if not any(
            isinstance(node, Interact)
            and "configurable" in node.model_dump_json().casefold()
            and "sku" in node.model_dump_json().casefold()
            for node in loop_nodes
        ):
            errors.append("fallback branch must establish the configurable parent SKU scope")
        if not any(
            isinstance(node, Read)
            and "material" in node.model_dump_json().casefold()
            for node in loop_nodes
        ):
            errors.append("ForEach must read material from product detail observations")
        final_compute_steps = [
            [step.op for step in node.steps]
            for node in program.statements
            if isinstance(node, Compute)
        ]
        if not any("filter" in steps and "distinct" in steps for steps in final_compute_steps):
            errors.append("resolved materials must be filtered and deduplicated after ForEach")
    return errors


def _print_program(program: Program) -> None:
    def visit(statements: list[Stmt], indent: str = "") -> None:
        for statement in statements:
            if isinstance(statement, (Interact, Acquire, Read, SourceCheck, Compute, Command)):
                print(f"{indent}[{statement.op}] {statement.id}: {statement.goal_text}")
            elif isinstance(statement, If):
                print(f"{indent}[if] {statement.cond.ref.var} {statement.cond.cmp}")
                visit(statement.then, indent + "  ")
                visit(statement.otherwise, indent + "  ")
            elif isinstance(statement, ForEach):
                print(f"{indent}[foreach] {statement.item} in {statement.items.var}")
                visit(statement.body, indent + "  ")
            else:
                print(f"{indent}[finish] {statement.message}")

    visit(program.statements)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    selected = [case for case in CASES if args.label.casefold() in case.label.casefold()]
    if not selected:
        print(f"no case matches label {args.label!r}")
        return 2

    failed = 0
    for case in selected:
        print(f"\n=== {case.label}: {case.goal}")
        try:
            knowledge = (
                load_knowledge_for_app(case.knowledge_app, "browser")
                if case.knowledge_app else None
            )
            program = decompose(
                case.goal,
                resolution=case.resolution,
                knowledge=knowledge.decompose_context(case.goal) if knowledge else "",
            )
            _print_program(program)
            errors = _check(case, program)
        except OrchestratorCompileError as exc:
            _print_program(exc.program)
            errors = [str(exc)]
        except Exception as exc:  # live provider/compiler boundary
            errors = [str(exc)]
        if errors:
            failed += 1
            for error in errors:
                print(f"  FAIL: {error}")
        else:
            print("  PASS")
    print(f"\nsemantic compiler eval: {len(selected) - failed}/{len(selected)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
