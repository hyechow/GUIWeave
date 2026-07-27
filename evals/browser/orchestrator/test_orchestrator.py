"""Static coding-orchestrator regression eval for query and form task groups.

The eval calls the real router and coding orchestrator, but never starts a browser.
Pass/fail is decided by deterministic compilation and task-contract assertions.

Run:
  uv run python evals/browser/orchestrator/test_orchestrator.py
  uv run python evals/browser/orchestrator/test_orchestrator.py --group query
  uv run python evals/browser/orchestrator/test_orchestrator.py --task 108 549
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import generate_code  # noqa: E402
from gui_agent.core.orchestrator.sandbox import validate_code  # noqa: E402
from gui_agent.core.router import resolve_intent  # noqa: E402
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app  # noqa: E402

CASES_FILE = Path(__file__).with_name("cases.json")
DATASET_FILE = (
    PROJECT_ROOT / "webarena-verified/assets/dataset/webarena-verified.json"
)
GROUP_ALIASES = {
    "query": "query_20260726",
    "form": "form_submission",
}
CTX_POSITIONS = {
    "reach": {"success": 1, "target": 2},
    "query": {"entity": 1, "fields": 2, "filters": 3},
    "read": {"target": 1, "fields": 2},
    "commit": {"target": 1, "values": 2},
}
_UNRESOLVED = object()


def load_cases() -> list[dict[str, Any]]:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    official = {
        item["task_id"]: item["intent"]
        for item in json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    }
    for case in cases:
        if official.get(case["task_id"]) != case["intent"]:
            raise ValueError(
                f"task {case['task_id']} intent differs from official WebArena data"
            )
    return cases


def _ctx_method(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
    ):
        return node.func.attr
    return None


def _argument(call: ast.Call, name: str) -> ast.AST | None:
    keyword = next(
        (item.value for item in call.keywords if item.arg == name),
        None,
    )
    if keyword is not None:
        return keyword
    position = CTX_POSITIONS.get(_ctx_method(call) or "", {}).get(name)
    return call.args[position] if position is not None and len(call.args) > position else None


def _literal(
    node: ast.AST | None,
    names: dict[str, Any] | None = None,
) -> Any:
    if node is None:
        return None
    if isinstance(node, ast.Name) and names is not None:
        return names.get(node.id, _UNRESOLVED)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_literal(item, names) for item in node.elts]
        if _UNRESOLVED in values:
            return _UNRESOLVED
        return (
            tuple(values)
            if isinstance(node, ast.Tuple)
            else set(values)
            if isinstance(node, ast.Set)
            else values
        )
    if isinstance(node, ast.Dict):
        keys = [_literal(item, names) for item in node.keys]
        values = [_literal(item, names) for item in node.values]
        if _UNRESOLVED in keys or _UNRESOLVED in values:
            return _UNRESOLVED
        return dict(zip(keys, values))
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return _UNRESOLVED


def _mapping_shape(
    node: ast.AST | None,
    names: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(node, ast.Dict):
        value = _literal(node, names)
        return value if isinstance(value, dict) else {}
    return {
        key.value: _literal(value, names)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _fields(
    node: ast.AST | None,
    names: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    value = _literal(node, names)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)], {}
    if isinstance(value, dict):
        return [str(key) for key in value], value
    shape = _mapping_shape(node, names)
    return list(shape), shape


def _call_records(tree: ast.AST) -> list[dict[str, Any]]:
    names: dict[str, Any] = {}
    for node in sorted(
        (
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.Assign, ast.AnnAssign))
        ),
        key=lambda item: (item.lineno, item.col_offset),
    ):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        value = _literal(node.value, names)
        if value is _UNRESOLVED:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names[target.id] = value

    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    records: list[dict[str, Any]] = []
    for call in sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _ctx_method(node)
        ),
        key=lambda node: (node.lineno, node.col_offset),
    ):
        method = _ctx_method(call) or ""
        fields, field_types = _fields(_argument(call, "fields"), names)
        target = _argument(call, "target")
        parent = parents.get(call)
        inside_if = False
        while parent is not None:
            if isinstance(parent, ast.If):
                inside_if = True
                break
            parent = parents.get(parent)
        records.append({
            "method": method,
            "line": call.lineno,
            "inside_if": inside_if,
            "entity": _literal(_argument(call, "entity"), names),
            "fields": fields,
            "field_types": field_types,
            "filters": _mapping_shape(_argument(call, "filters"), names),
            "values": _mapping_shape(_argument(call, "values"), names),
            "success": _mapping_shape(_argument(call, "success"), names),
            "target_mode": (
                "none"
                if target is None
                or isinstance(target, ast.Constant) and target.value is None
                else "present"
            ),
        })
    return records


def _subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _subset(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def _matches(record: dict[str, Any], spec: dict[str, Any]) -> bool:
    if record["method"] != spec["method"]:
        return False
    for key in ("entity", "inside_if", "target_mode"):
        if key in spec and record[key] != spec[key]:
            return False
    for spec_key, record_key in (
        ("fields_include", "fields"),
        ("filter_keys", "filters"),
        ("value_keys", "values"),
        ("success_keys", "success"),
    ):
        if spec_key in spec and not set(spec[spec_key]) <= set(record[record_key]):
            return False
    for spec_key, record_key in (
        ("field_types_include", "field_types"),
        ("filters_include", "filters"),
        ("values_include", "values"),
        ("success_include", "success"),
    ):
        if spec_key in spec and not _subset(record[record_key], spec[spec_key]):
            return False
    return True


def _literal_values(tree: ast.AST) -> list[Any]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
    ]


def _has_literal(values: list[Any], expected: Any) -> bool:
    return any(
        value == expected
        and (
            type(value) is type(expected)
            or isinstance(value, (int, float))
            and not isinstance(value, bool)
            and isinstance(expected, (int, float))
            and not isinstance(expected, bool)
        )
        for value in values
    )


def _date_values(tree: ast.AST) -> set[str]:
    """Collect statically declared dates from strings and date constructors."""
    result: set[str] = set()
    for value in _literal_values(tree):
        if not isinstance(value, str):
            continue
        for parser in (
            lambda text: date.fromisoformat(text),
            lambda text: datetime.strptime(text, "%m/%d/%Y").date(),
        ):
            try:
                result.add(parser(value).isoformat())
                break
            except ValueError:
                continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        values = [_literal(item) for item in node.args[:3]]
        if (
            name in {"date", "datetime"}
            and len(values) == 3
            and all(isinstance(value, int) for value in values)
        ):
            try:
                result.add(date(*values).isoformat())
            except ValueError:
                continue
    return result


def evaluate_source(source: str, contract: dict[str, Any]) -> list[str]:
    diagnostics = validate_code(source)
    failures = [item.render() for item in diagnostics]
    if diagnostics:
        return failures
    tree = ast.parse(source)
    records = _call_records(tree)
    counts = Counter(record["method"] for record in records)

    for method, expected in contract.get("method_counts", {}).items():
        if counts[method] != expected:
            failures.append(
                f"METHOD_COUNT:{method}: expected={expected} actual={counts[method]}"
            )
    for method, minimum in contract.get("method_min_counts", {}).items():
        if counts[method] < minimum:
            failures.append(
                f"METHOD_MIN:{method}: expected>={minimum} actual={counts[method]}"
            )
    for index, spec in enumerate(contract.get("required_calls", []), 1):
        if not any(_matches(record, spec) for record in records):
            failures.append(f"REQUIRED_CALL:{index}:{spec}")
    for index, options in enumerate(contract.get("required_any_calls", []), 1):
        if not any(
            _matches(record, option)
            for option in options
            for record in records
        ):
            failures.append(f"REQUIRED_ANY_CALL:{index}:{options}")

    cursor = 0
    for index, spec in enumerate(contract.get("ordered_calls", []), 1):
        match = next(
            (
                position
                for position in range(cursor, len(records))
                if _matches(records[position], spec)
            ),
            None,
        )
        if match is None:
            failures.append(f"ORDERED_CALL:{index}:{spec}")
            break
        cursor = match + 1

    returns_value = any(
        isinstance(node, ast.Return)
        and node.value is not None
        and not (
            isinstance(node.value, ast.Constant)
            and node.value.value is None
        )
        for node in ast.walk(tree)
    )
    ranks = any(
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name) and node.func.id == "sorted"
            or isinstance(node.func, ast.Attribute) and node.func.attr == "sort"
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in {"min", "max"}
                and any(keyword.arg == "key" for keyword in node.keywords)
            )
        )
        for node in ast.walk(tree)
    )
    features = {
        "returns_value": returns_value,
        "no_return_value": not returns_value,
        "sorts": ranks,
    }
    for feature in contract.get("features", []):
        if not features.get(feature, False):
            failures.append(f"FEATURE:{feature}")

    slice_stops = {
        node.slice.upper.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Slice)
        and isinstance(node.slice.upper, ast.Constant)
        and isinstance(node.slice.upper.value, int)
    }
    for stop in contract.get("slice_stops", []):
        if stop not in slice_stops:
            failures.append(f"SLICE_STOP:{stop}")

    literals = _literal_values(tree)
    for value in contract.get("literal_values", []):
        if not _has_literal(literals, value):
            failures.append(f"LITERAL_REQUIRED:{value!r}")
    for value in contract.get("forbidden_literals", []):
        if _has_literal(literals, value):
            failures.append(f"LITERAL_FORBIDDEN:{value!r}")
    declared_dates = _date_values(tree)
    for value in contract.get("date_values", []):
        if value not in declared_dates:
            failures.append(f"DATE_REQUIRED:{value!r}")
    return failures


def _select_cases(
    cases: list[dict[str, Any]],
    *,
    group: str,
    task_ids: list[int],
) -> list[dict[str, Any]]:
    selected = cases
    if group != "all":
        selected = [
            case for case in selected
            if case["group"] == GROUP_ALIASES[group]
        ]
    if task_ids:
        wanted = set(task_ids)
        selected = [case for case in selected if case["task_id"] in wanted]
        missing = wanted - {case["task_id"] for case in selected}
        if missing:
            raise ValueError(f"task ids are absent from selected eval group: {sorted(missing)}")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["all", *GROUP_ALIASES], default="all")
    parser.add_argument("--task", nargs="*", type=int, default=[])
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    cases = _select_cases(
        load_cases(),
        group=args.group,
        task_ids=args.task,
    )
    if args.list:
        for case in cases:
            print(f"{case['group']:18s} {case['task_id']}: {case['intent']}")
        return 0

    knowledge = load_knowledge_for_app("shopping_admin", "browser")
    output_dir = (
        PROJECT_ROOT / "logs/orchestrator_eval" / time.strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failed = 0
    for case in cases:
        samples = []
        for sample_index in range(args.k):
            resolution = resolve_intent(case["intent"])
            plan = generate_code(
                case["intent"],
                knowledge=(
                    knowledge.orchestrator_context(case["intent"])
                    if knowledge else ""
                ),
                resolution=resolution,
                current_site="shopping_admin",
            )
            failures = evaluate_source(plan.source, case["contract"])
            if not plan.executable:
                failures.insert(0, "PLAN_NOT_EXECUTABLE")
            failed += bool(failures)
            mark = "PASS" if not failures else "FAIL"
            print(
                f"[{mark}] {case['group']} task={case['task_id']} "
                f"sample={sample_index + 1}/{args.k}",
                flush=True,
            )
            if failures:
                print("       " + "; ".join(failures[:4]), flush=True)
            samples.append({
                "sample": sample_index + 1,
                "ok": not failures,
                "failures": failures,
                "executable": plan.executable,
                "repaired": plan.repaired,
                "resolution": resolution.model_dump(),
                "source": plan.source,
                "attempts": [
                    {
                        "source": attempt.source,
                        "diagnostics": [
                            item.render() for item in attempt.diagnostics
                        ],
                        "run_ok": (
                            attempt.run.ok
                            if attempt.run is not None
                            else None
                        ),
                        "run_error": (
                            attempt.run.error
                            if attempt.run is not None
                            else ""
                        ),
                    }
                    for attempt in plan.attempts
                ],
            })
        results.append({**case, "samples": samples})
        (output_dir / "report.json").write_text(
            json.dumps({"cases": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    total = len(cases) * args.k
    print(f"\n{total - failed}/{total} samples passed")
    print(f"report -> {output_dir / 'report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
