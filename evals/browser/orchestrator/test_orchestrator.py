"""Static coding-orchestrator eval for WebArena-Verified hard single-site tasks.

Coverage: shopping_admin (55) + shopping (56) from the official hard subset
(`webarena-verified/assets/dataset/subsets/webarena-verified-hard.json`), pure
single-site only. Calls the real router + coding orchestrator; never starts a
browser.

Scoring (only two bars matter for pass/fail):
  - **all 111**: must be executable (`validate_code` + plan.executable)
  - **curated 14**: also match the hand-written AST `contract`
  - non-curated cases still carry an inferred `contract` (from the baseline
    program) for reuse/inspection, but it is **not** scored

Baseline (qwen3.7-plus/tokenplan, 2026-08-05) is on each case as `baseline`
and in `baseline_qwen37_tokenplan_20260805.json` / `BASELINE.md`.
Use `--compare-baseline` to print regressions/improvements.

Run:
  uv run python evals/browser/orchestrator/test_orchestrator.py
  uv run python evals/browser/orchestrator/test_orchestrator.py --group admin
  uv run python evals/browser/orchestrator/test_orchestrator.py --group shopping
  uv run python evals/browser/orchestrator/test_orchestrator.py --group curated
  uv run python evals/browser/orchestrator/test_orchestrator.py --task 108 549
  uv run python evals/browser/orchestrator/test_orchestrator.py -j 5
  uv run python evals/browser/orchestrator/test_orchestrator.py --compare-baseline
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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
HARD_SUBSET_FILE = (
    PROJECT_ROOT
    / "webarena-verified/assets/dataset/subsets/webarena-verified-hard.json"
)
GROUP_ALIASES = {
    "admin": "hard_shopping_admin",
    "shopping": "hard_shopping",
    "curated": "__curated__",
}
DEFAULT_SITE = "shopping_admin"
BASELINE_FILE = Path(__file__).with_name("baseline_qwen37_tokenplan_20260805.json")
CTX_POSITIONS = {
    "reach": {"state": 0, "goal": 1, "success": 2, "target": 3},
    "query": {"state": 0, "entity": 1, "filters": 2, "coverage": 3},
    "acquire": {"scope": 0, "fields": 1, "coverage": 2},
    "read": {"state": 0, "target": 1, "fields": 2, "restore": 3},
    "commit": {"state": 0, "goal": 1, "target": 2, "values": 3},
    "command": {"state": 0, "capability": 1},
}
_UNRESOLVED = object()


def load_cases() -> list[dict[str, Any]]:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    official = {
        item["task_id"]: item
        for item in json.loads(DATASET_FILE.read_text(encoding="utf-8"))
    }
    hard_ids = set(
        json.loads(HARD_SUBSET_FILE.read_text(encoding="utf-8"))["task_ids"]
    )
    for case in cases:
        task = official.get(case["task_id"])
        if task is None:
            raise ValueError(f"task {case['task_id']} missing from official dataset")
        if task["intent"] != case["intent"]:
            raise ValueError(
                f"task {case['task_id']} intent differs from official WebArena data"
            )
        site = case.get("site") or (
            task["sites"][0] if task.get("sites") == [task["sites"][0]] else None
        )
        if task.get("sites") != [site]:
            raise ValueError(
                f"task {case['task_id']} is not pure single-site {site!r}: "
                f"{task.get('sites')!r}"
            )
        if case["task_id"] not in hard_ids:
            raise ValueError(
                f"task {case['task_id']} is outside the official hard subset"
            )
        case.setdefault("site", site)
        case.setdefault("contract", {})
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


def _referenced_fields(node: ast.AST | None) -> set[str]:
    """Field names accessed as ``X[\"field\"]`` inside a node subtree.

    Used to check that every field a commit's values reference was produced by an
    earlier read/acquire — otherwise the program raises KeyError at runtime.
    """
    if node is None:
        return set()
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.slice, ast.Constant)
            and isinstance(child.slice.value, str)
        ):
            found.add(child.slice.value)
    return found


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
        inside_loop = False
        while parent is not None:
            if isinstance(parent, ast.If):
                inside_if = True
            if isinstance(parent, (ast.For, ast.AsyncFor, ast.While)):
                inside_loop = True
            parent = parents.get(parent)
        records.append({
            "method": method,
            "line": call.lineno,
            "inside_if": inside_if,
            "inside_loop": inside_loop,
            "entity": _literal(_argument(call, "entity"), names),
            "fields": fields,
            "field_types": field_types,
            "coverage": (
                _literal(_argument(call, "coverage"), names)
                if method in {"query", "acquire"}
                and _argument(call, "coverage") is not None
                else "complete" if method in {"query", "acquire"} else None
            ),
            "filters": _mapping_shape(_argument(call, "filters"), names),
            "values": _mapping_shape(_argument(call, "values"), names),
            "success": _mapping_shape(_argument(call, "success"), names),
            "referenced_fields": sorted(
                _referenced_fields(_argument(call, "values"))
            ),
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
    for key in ("entity", "inside_if", "inside_loop", "target_mode", "coverage"):
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
    if (
        "filter_keys_exact" in spec
        and set(record["filters"]) != set(spec["filter_keys_exact"])
    ):
        return False
    if (
        "value_keys_exact" in spec
        and set(record["values"]) != set(spec["value_keys_exact"])
    ):
        return False
    for spec_key, record_key in (
        ("field_types_include", "field_types"),
        ("filters_include", "filters"),
        ("values_include", "values"),
        ("success_include", "success"),
    ):
        if spec_key in spec and not _subset(record[record_key], spec[spec_key]):
            return False
    if "field_type_any" in spec:
        types = (
            set(record["field_types"].values())
            if isinstance(record["field_types"], dict)
            else set()
        )
        if spec["field_type_any"] not in types:
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


def _read_data_flows_to_commit_goal(tree: ast.AST) -> bool:
    def assigned_names(target: ast.AST) -> set[str]:
        return {
            item.id
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        }

    derived: set[str] = set()
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for node in assignments:
        value = node.value
        if isinstance(value, ast.Call) and _ctx_method(value) == "read":
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            derived.update(
                name for target in targets for name in assigned_names(target)
            )
    changed = True
    while changed:
        changed = False
        for node in assignments:
            if not any(
                isinstance(item, ast.Name) and item.id in derived
                for item in ast.walk(node.value)
            ):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in assigned_names(target) - derived:
                    derived.add(name)
                    changed = True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _ctx_method(node) != "commit":
            continue
        goal = node.args[0] if node.args else next(
            (item.value for item in node.keywords if item.arg == "goal"),
            None,
        )
        if goal is not None and any(
            isinstance(item, ast.Name) and item.id in derived
            for item in ast.walk(goal)
        ):
            return True
    return False


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

    alternatives = contract.get("ordered_call_alternatives", [])
    if alternatives:
        def matches_sequence(sequence: list[dict[str, Any]]) -> bool:
            cursor = 0
            for spec in sequence:
                match = next(
                    (
                        position
                        for position in range(cursor, len(records))
                        if _matches(records[position], spec)
                    ),
                    None,
                )
                if match is None:
                    return False
                cursor = match + 1
            return True

        if not any(matches_sequence(sequence) for sequence in alternatives):
            failures.append("ORDERED_CALL_ALTERNATIVES")

    # Data-flow: every field a commit's values reference must have been produced
    # by an earlier read/acquire, otherwise the program raises KeyError at runtime.
    produced: set[str] = set()
    for record in records:
        if record["method"] in {"read", "acquire"}:
            produced.update(record["fields"])
        elif record["method"] == "commit":
            for field in record["referenced_fields"]:
                if field not in produced:
                    failures.append(
                        f"DATA_FLOW: commit references {field!r} that no "
                        "earlier read/acquire produced"
                    )

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
    asserts_exactly_one = any(
        isinstance(node, ast.Assert)
        and any(
            isinstance(expr, ast.Compare)
            and any(isinstance(op, ast.Eq) for op in expr.ops)
            and any(
                isinstance(item, ast.Constant) and item.value == 1
                for item in (expr.left, *expr.comparators)
            )
            and any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "len"
                for item in ast.walk(expr)
            )
            for expr in ast.walk(node.test)
        )
        for node in ast.walk(tree)
    )
    semantic_boolean_fields = {
        str(field)
        for record in records
        if record["method"] == "read" and record["inside_loop"]
        for field, value in record["field_types"].items()
        if value == "boolean"
    }
    if_test_literals = {
        node.value
        for branch in ast.walk(tree)
        if isinstance(branch, ast.If)
        for node in ast.walk(branch.test)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    features = {
        "returns_value": returns_value,
        "no_return_value": not returns_value,
        "sorts": ranks,
        "selects_first": any(
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == 0
            for node in ast.walk(tree)
        ),
        "asserts_exactly_one": asserts_exactly_one,
        "verifies_semantic_match": bool(
            semantic_boolean_fields & if_test_literals
        ),
        "read_data_flows_to_commit_goal": _read_data_flows_to_commit_goal(tree),
        "sums": any(
            (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "sum"
            )
            or isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
            for node in ast.walk(tree)
        ),
        "returns_integer": any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "int"
            for node in ast.walk(tree)
        ),
    }
    for feature in contract.get("features", []):
        if not features.get(feature, False):
            failures.append(f"FEATURE:{feature}")
    for feature in contract.get("forbidden_features", []):
        if features.get(feature, False):
            failures.append(f"FEATURE_FORBIDDEN:{feature}")

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
    for value in contract.get("literal_substrings", []):
        if not any(isinstance(item, str) and value in item for item in literals):
            failures.append(f"LITERAL_SUBSTRING_REQUIRED:{value!r}")
    for value in contract.get("forbidden_literals", []):
        if _has_literal(literals, value):
            failures.append(f"LITERAL_FORBIDDEN:{value!r}")
    declared_dates = _date_values(tree)
    for value in contract.get("date_values", []):
        if value not in declared_dates:
            failures.append(f"DATE_REQUIRED:{value!r}")
    return failures


def grade_sample(case: dict[str, Any], sample: dict[str, Any]) -> str:
    """Classify one sample for baseline comparison / reporting.

    Scored bars:
      - curated: contract_pass | contract_fail | executable_fail
      - others:  executable_pass | executable_fail

    Inferred contracts on non-curated cases are annotations only.
    """
    curated = bool(case.get("curated"))
    executable = bool(sample.get("executable"))
    ok = bool(sample.get("ok"))
    if curated:
        if not executable:
            return "executable_fail"
        return "contract_pass" if ok else "contract_fail"
    return "executable_pass" if executable else "executable_fail"


def _failure_codes(failures: list[str]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for item in failures:
        if item.startswith("["):
            code = item.split("]")[0].strip("[]").split()[0]
        else:
            code = item.split(":")[0]
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _select_cases(
    cases: list[dict[str, Any]],
    *,
    group: str,
    task_ids: list[int],
) -> list[dict[str, Any]]:
    selected = cases
    if group != "all":
        alias = GROUP_ALIASES[group]
        if alias == "__curated__":
            selected = [case for case in selected if case.get("curated")]
        else:
            selected = [case for case in selected if case["group"] == alias]
    if task_ids:
        wanted = set(task_ids)
        selected = [case for case in selected if case["task_id"] in wanted]
        missing = wanted - {case["task_id"] for case in selected}
        if missing:
            raise ValueError(f"task ids are absent from selected eval group: {sorted(missing)}")
    return selected


def _run_sample(
    case: dict[str, Any],
    *,
    sample_index: int,
    knowledge: Any,
    site: str,
    best_of: int = 1,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Generate + grade one sample. Safe for thread-pool workers.

    With ``best_of`` > 1, generate that many independent plans and keep the one
    with the fewest contract failures (preferring an executable program), which
    absorbs per-generation variance.
    """
    # Score contract only for curated cases. Inferred contracts stay on the case
    # for reuse but do not affect pass/fail.
    score_contract = bool(case.get("curated"))
    contract = case.get("contract") or {} if score_contract else {}

    def _score(case_intent: str, knowledge_text: Any, site_text: str) -> dict[str, Any]:
        resolution = resolve_intent(case_intent)
        plan = generate_code(
            case_intent,
            knowledge=(
                knowledge_text.orchestrator_context(case_intent)
                if knowledge_text else ""
            ),
            resolution=resolution,
            current_site=site_text,
            temperature=temperature,
        )
        failures = evaluate_source(plan.source, contract)
        if not plan.executable:
            failures.insert(0, "PLAN_NOT_EXECUTABLE")
        return {"plan": plan, "resolution": resolution, "failures": failures}

    candidates = [
        _score(case["intent"], knowledge, site)
        for _ in range(max(1, best_of))
    ]
    # Prefer zero failures, then executable, then fewest failure codes.
    candidates.sort(key=lambda c: (
        bool(c["failures"]),
        not c["plan"].executable,
        len(c["failures"]),
    ))
    scored = candidates[0]
    plan = scored["plan"]
    resolution = scored["resolution"]
    failures = scored["failures"]
    # Optional annotation check (not scored): does the program still match the
    # inferred/baseline contract?
    annotation_failures: list[str] = []
    if not score_contract and case.get("contract"):
        annotation_failures = evaluate_source(
            plan.source, case.get("contract") or {}
        )
    sample = {
        "sample": sample_index + 1,
        "ok": not failures,
        "failures": failures,
        "failure_codes": _failure_codes(failures),
        "annotation_failures": annotation_failures,
        "executable": plan.executable,
        "repaired": plan.repaired,
        "grade": "",  # filled below once dict exists
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
    }
    sample["grade"] = grade_sample(case, sample)
    return sample


def _summary(
    *,
    group_filter: str,
    cases: list[dict[str, Any]],
    results: list[dict[str, Any] | None],
    k: int,
    jobs: int,
) -> dict[str, Any]:
    finished = [item for item in results if item is not None]
    samples = [
        sample
        for item in finished
        for sample in item.get("samples", [])
    ]
    by_grade = Counter(str(sample.get("grade") or "") for sample in samples)
    curated_finished = [item for item in finished if item.get("curated")]
    curated_samples = [
        sample
        for item in curated_finished
        for sample in item.get("samples", [])
    ]
    return {
        "group_filter": group_filter,
        "total_cases": len(cases),
        "finished_cases": len(finished),
        "k": k,
        "jobs": jobs,
        # Primary bars: executable (all) + curated contract (subset).
        "samples_passed": sum(1 for sample in samples if sample.get("ok")),
        "samples_total": len(cases) * k,
        "samples_finished": len(samples),
        "executable_ok": sum(1 for sample in samples if sample.get("executable")),
        "curated_passed": sum(1 for sample in curated_samples if sample.get("ok")),
        "curated_total": len(curated_finished) * k,
        "by_grade": dict(by_grade),
        "by_site": {
            site_name: {
                "cases": sum(
                    1 for item in finished if item.get("site") == site_name
                ),
                "executable": sum(
                    1
                    for item in finished
                    if item.get("site") == site_name
                    and item.get("samples")
                    and all(sample.get("executable") for sample in item["samples"])
                ),
                "passed": sum(
                    1
                    for item in finished
                    if item.get("site") == site_name
                    and item.get("samples")
                    and all(sample["ok"] for sample in item["samples"])
                ),
            }
            for site_name in sorted(
                {
                    str(item.get("site") or DEFAULT_SITE)
                    for item in finished
                }
            )
        },
    }


def _compare_to_baseline(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Diff current results against per-case `baseline` annotations."""
    by_id = {case["task_id"]: case for case in results}
    improved: list[dict[str, Any]] = []
    regressed: list[dict[str, Any]] = []
    unchanged = 0
    missing_baseline = 0
    for case in cases:
        baseline = case.get("baseline") or {}
        if not baseline:
            missing_baseline += 1
            continue
        result = by_id.get(case["task_id"])
        if result is None or not result.get("samples"):
            continue
        sample = result["samples"][0]
        before = bool(baseline.get("ok"))
        after = bool(sample.get("ok"))
        entry = {
            "task_id": case["task_id"],
            "site": case.get("site"),
            "baseline_grade": baseline.get("grade"),
            "grade": sample.get("grade"),
            "baseline_ok": before,
            "ok": after,
            "failure_codes": sample.get("failure_codes") or [],
        }
        if before == after:
            unchanged += 1
        elif after and not before:
            improved.append(entry)
        else:
            regressed.append(entry)
    return {
        "baseline_run": next(
            (
                (case.get("baseline") or {}).get("run")
                for case in cases
                if case.get("baseline")
            ),
            None,
        ),
        "unchanged": unchanged,
        "improved": improved,
        "regressed": regressed,
        "missing_baseline": missing_baseline,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=["all", *GROUP_ALIASES], default="all")
    parser.add_argument("--task", nargs="*", type=int, default=[])
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument(
        "--best-of",
        type=int,
        default=1,
        help="generate N plans per sample and keep the best-scoring one (default 1)",
    )
    parser.add_argument(
        "--temp",
        type=float,
        default=0.0,
        help="LLM sampling temperature for code generation (default 0)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=5,
        help="max concurrent case workers (default 5, capped at 5)",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--compare-baseline",
        action="store_true",
        help="after the run, diff ok/grade against case['baseline'] annotations",
    )
    args = parser.parse_args()
    jobs = max(1, min(5, args.jobs))

    cases = _select_cases(
        load_cases(),
        group=args.group,
        task_ids=args.task,
    )
    if args.list:
        for case in cases:
            baseline = case.get("baseline") or {}
            tags = []
            if case.get("curated"):
                tags.append("curated")
            if baseline.get("grade"):
                tags.append(str(baseline["grade"]))
            tag = f" [{' '.join(tags)}]" if tags else ""
            print(
                f"{case['group']:22s} {case['site']:15s} "
                f"{case['task_id']:>4d}{tag}: {case['intent']}"
            )
        return 0

    knowledge_by_site: dict[str, Any] = {}
    for case in cases:
        site = str(case.get("site") or DEFAULT_SITE)
        if site not in knowledge_by_site:
            knowledge_by_site[site] = load_knowledge_for_app(site, "browser")

    output_dir = (
        PROJECT_ROOT / "logs/orchestrator_eval" / time.strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    # Preserve input order in the final report.
    results: list[dict[str, Any] | None] = [None] * len(cases)
    print_lock = threading.Lock()
    write_lock = threading.Lock()
    done = 0

    def work(index: int, case: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        site = str(case.get("site") or DEFAULT_SITE)
        knowledge = knowledge_by_site[site]
        samples: list[dict[str, Any]] = []
        for sample_index in range(args.k):
            try:
                samples.append(
                    _run_sample(
                        case,
                        sample_index=sample_index,
                        knowledge=knowledge,
                        site=site,
                        best_of=args.best_of,
                        temperature=args.temp,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — isolate worker crashes
                sample = {
                    "sample": sample_index + 1,
                    "ok": False,
                    "failures": [f"WORKER_ERROR:{type(exc).__name__}: {exc}"],
                    "failure_codes": ["WORKER_ERROR"],
                    "annotation_failures": [],
                    "executable": False,
                    "repaired": False,
                    "grade": "executable_fail",
                    "resolution": {},
                    "source": "",
                    "attempts": [],
                }
                samples.append(sample)
        return index, {**case, "samples": samples}

    def emit(index: int, result: dict[str, Any]) -> None:
        nonlocal done
        site = str(result.get("site") or DEFAULT_SITE)
        with print_lock:
            for sample in result["samples"]:
                mark = "PASS" if sample["ok"] else "FAIL"
                grade = sample.get("grade") or ""
                print(
                    f"[{mark}] {index + 1}/{len(cases)} {result['group']} "
                    f"site={site} task={result['task_id']} "
                    f"grade={grade} sample={sample['sample']}/{args.k}",
                    flush=True,
                )
                if sample["failures"]:
                    print(
                        "       " + "; ".join(sample["failures"][:4]),
                        flush=True,
                    )
            done += 1
        with write_lock:
            results[index] = result
            payload = {
                "summary": _summary(
                    group_filter=args.group,
                    cases=cases,
                    results=results,
                    k=args.k,
                    jobs=jobs,
                ),
                "cases": [item for item in results if item is not None],
            }
            (output_dir / "report.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if jobs == 1 or len(cases) <= 1:
        for index, case in enumerate(cases):
            emit(index, work(index, case)[1])
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(work, index, case): index
                for index, case in enumerate(cases)
            }
            for future in as_completed(futures):
                index, result = future.result()
                emit(index, result)

    finished = [item for item in results if item is not None]
    samples = [
        sample
        for item in finished
        for sample in item.get("samples", [])
    ]
    failed = sum(1 for sample in samples if not sample.get("ok"))
    executable_ok = sum(1 for sample in samples if sample.get("executable"))
    total = len(cases) * args.k
    by_grade = Counter(str(sample.get("grade") or "") for sample in samples)
    curated_total = sum(1 for case in cases if case.get("curated")) * args.k
    curated_passed = sum(
        1
        for item in finished
        if item.get("curated")
        for sample in item.get("samples", [])
        if sample.get("ok")
    )
    print(f"\n{total - failed}/{total} samples passed")
    print(f"executable {executable_ok}/{total}")
    print(f"curated {curated_passed}/{curated_total}")
    print(f"grades {dict(by_grade)}")
    print(f"jobs={jobs}")
    print(f"report -> {output_dir / 'report.json'}")

    if args.compare_baseline:
        comparison = _compare_to_baseline(cases, finished)
        print(
            f"baseline {comparison['baseline_run']}: "
            f"unchanged={comparison['unchanged']} "
            f"improved={len(comparison['improved'])} "
            f"regressed={len(comparison['regressed'])} "
            f"missing={comparison['missing_baseline']}"
        )
        for item in comparison["regressed"][:20]:
            print(
                f"  REGRESS task={item['task_id']} "
                f"{item['baseline_grade']} -> {item['grade']} "
                f"{item['failure_codes']}"
            )
        for item in comparison["improved"][:20]:
            print(
                f"  IMPROVE task={item['task_id']} "
                f"{item['baseline_grade']} -> {item['grade']}"
            )
        payload = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
        payload["baseline_comparison"] = comparison
        (output_dir / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
