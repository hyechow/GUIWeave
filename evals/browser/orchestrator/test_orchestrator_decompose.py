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

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.core.orchestrator import (
    Acquire,
    Command,
    Data,
    Finish,
    ForEach,
    If,
    Interact,
    Program,
    decompose,
    validate_program,
)
from gui_agent.core.orchestrator.program import Stmt
from gui_agent.core.router import EntityRef, IntentResolution


@dataclass(frozen=True)
class Case:
    label: str
    goal: str
    required_text: tuple[str, ...] = ()
    required_data_text: tuple[str, ...] = ()
    expect_foreach: bool = False
    # Finish-consumed numbers must be defined by Data (AST/dataflow, not word lists).
    expect_finish_number_from_data: bool = False
    expect_materialized_data: bool = False
    required_materialized_text: tuple[str, ...] = ()
    resolution: IntentResolution | None = None
    expect_lookup_fallback: bool = False


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
        expect_finish_number_from_data=True,
    ),
    Case(
        "rank-customer",
        "Get customer email(s) who completed the second most number of orders in the entire history.",
        expect_finish_number_from_data=False,  # answer is a tie-preserving record list, not a number
        expect_materialized_data=True,
    ),
    Case(
        "data-top-n",
        "From the current result data, return the top 2 labels ranked by usage.",
        required_data_text=("top 2",),
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
    """Map bind name → producing op (interact/data/command). Ignores If/ForEach merges."""
    origins: dict[str, str] = {}
    for node in _walk(statements):
        if isinstance(node, (Interact, Acquire, Data, Command)) and node.bind:
            origins[node.bind] = node.op
    return origins


def _check_finish_numbers_from_data(program: Program) -> list[str]:
    """AST invariant: every Finish-cited number field's bind is produced by Data."""
    errors: list[str] = []
    origins = _bind_origins(program.statements)
    returns_by_bind: dict[str, dict] = {}
    for node in _walk(program.statements):
        if isinstance(node, (Interact, Acquire, Data, Command)) and node.bind:
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
            if origin != "data":
                errors.append(
                    f"finish.outputs.{out_name} cites number {ref.var}.{field} "
                    f"from op={origin!r}; expected Data (FINISH_NUMERIC_FROM_DATA)"
                )
    return errors


def _check(case: Case, program: Program) -> list[str]:
    errors = [f"{issue.code}: {issue}" for issue in validate_program(program)]
    nodes = list(_walk(program.statements))
    executors = [node for node in nodes if isinstance(node, (Interact, Acquire, Data, Command))]
    if not executors:
        errors.append("Program contains no executor-backed statement")
    payload = program.model_dump_json().casefold()
    for value in case.required_text:
        if value.casefold() not in payload:
            errors.append(f"required value was dropped: {value!r}")
    data_payload = " ".join(node.goal for node in nodes if isinstance(node, Data)).casefold()
    for value in case.required_data_text:
        if value.casefold() not in data_payload:
            errors.append(f"Data goal dropped semantic constant: {value!r}")
    if case.expect_foreach and not any(isinstance(node, ForEach) for node in nodes):
        errors.append("expected explicit ForEach over materialized data")
    for loop in (node for node in nodes if isinstance(node, ForEach)):
        if not loop.body:
            errors.append("ForEach body is empty")
        if any(hasattr(loop, field) for field in ("body_goal", "member_desc")):
            errors.append("ForEach exposes retired runtime expansion fields")
    # Always enforce the structural number gate on live programs (same as compile-time).
    errors.extend(_check_finish_numbers_from_data(program))
    if case.expect_finish_number_from_data:
        if not any(isinstance(node, Data) for node in nodes):
            errors.append("count/aggregate goal expected at least one Data statement")
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
                    if isinstance(n, (Interact, Acquire, Data, Command)) and n.bind == ref.var:
                        bind_returns = dict(n.returns)
                        break
                spec = bind_returns.get(ref.path[0])
                if spec is not None and getattr(spec, "type", None) == "number":
                    has_finish_number = True
        if not has_finish_number:
            errors.append("expected Finish to cite a number field from Data")
    if case.label == "rank-customer":
        data_nodes = [node for node in nodes if isinstance(node, Data) and node.mode == "derive"]
        if not data_nodes:
            errors.append("rank/group goal expected a Data statement for ranking/aggregation")
        rank_outputs = [spec for node in data_nodes for spec in node.returns.values()]
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
            for node in data_nodes
        ):
            errors.append("rank Data must require a customer email source field")
        source_fields = {
            field.casefold()
            for node in data_nodes
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
        data_inputs = {
            ref.var
            for node in nodes
            if isinstance(node, Data)
            for ref in node.inputs.values()
        }
        unchecked_acquires = [
            node.id
            for node in nodes
            if isinstance(node, Acquire) and node.source_check is None
        ]
        if not collection_binds:
            errors.append("entire-history ranking requires complete Acquire collection output")
        elif collection_binds.isdisjoint(data_inputs):
            errors.append("Data must consume the materialized complete collection via inputs")
        if unchecked_acquires:
            errors.append(
                "Acquire must consume a Data inspect availability check: "
                + ", ".join(unchecked_acquires)
            )
        collection_contract = " ".join(spec.description for spec in collection_specs).casefold()
        for value in case.required_materialized_text:
            if value.casefold() not in collection_contract:
                errors.append(
                    f"complete collection contract dropped required field: {value!r}"
                )
        data_record_specs = [
            spec
            for node in nodes
            if isinstance(node, Data)
            for spec in node.returns.values()
            if spec.type == "list[record]"
        ]
        if data_record_specs and any(not spec.fields for spec in data_record_specs):
            errors.append("Data list[record] output must declare fields")
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
    return errors


def _print_program(program: Program) -> None:
    def visit(statements: list[Stmt], indent: str = "") -> None:
        for statement in statements:
            if isinstance(statement, (Interact, Acquire, Data, Command)):
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
            program = decompose(case.goal, resolution=case.resolution)
            _print_program(program)
            errors = _check(case, program)
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
