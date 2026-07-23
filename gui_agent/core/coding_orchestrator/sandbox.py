"""Restricted source validation and fixture execution for coding plans.

This is an experiment boundary, not a production-grade hostile-code sandbox.
The child process, AST allow-list and timeout keep evaluation failures isolated.
"""

from __future__ import annotations

import ast
import copy
import datetime
import math
import multiprocessing
import queue
import re
import time as _time
import traceback
import _strptime
from dataclasses import dataclass, field
from typing import Any

from gui_agent.core.schemas import StatementContract

from .models import CodeDiagnostic, CodingRunResult, TraceEvent, WriteEvent


CTX_METHODS = frozenset({"interact", "lookup", "acquire", "read", "compute", "command"})
SAFE_MODULES = {"datetime": datetime, "math": math}
_RUNTIME_MODULES = {"_strptime": _strptime, "time": _time}


def _safe_import(
    name: str,
    globals: Any = None,
    locals: Any = None,
    fromlist: tuple[str, ...] = (),
    level: int = 0,
) -> Any:
    del globals, locals, fromlist
    if name in _RUNTIME_MODULES:
        return _RUNTIME_MODULES[name]
    if level or name not in SAFE_MODULES:
        raise ImportError(f"module {name!r} is not available to coding plans")
    return SAFE_MODULES[name]


SAFE_BUILTINS = {
    "__import__": _safe_import,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "KeyError": KeyError,
    "TypeError": TypeError,
    "ValueError": ValueError,
    "zip": zip,
}
SAFE_METHODS = frozenset({
    "append", "casefold", "count", "endswith", "extend", "find", "fromisoformat",
    "format", "get", "index", "items", "join", "keys", "lower", "lstrip", "partition",
    "removeprefix", "removesuffix", "replace", "reverse", "rfind", "rpartition",
    "rsplit", "rstrip", "sort", "split", "splitlines", "startswith", "strftime",
    "strip", "strptime", "upper", "values", "zfill",
})
SAFE_PROPERTIES = frozenset({"day", "hour", "microsecond", "minute", "month", "second", "year"})


@dataclass(frozen=True)
class FixtureSpec:
    lookups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reads: dict[str, dict[str, Any]] = field(default_factory=dict)
    command_results: dict[str, Any] = field(default_factory=dict)
    compute_results: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LookupScope:
    entity: str
    field: str
    fallback: str = ""


def _diag(node: ast.AST, code: str, message: str) -> CodeDiagnostic:
    return CodeDiagnostic(
        code=code,
        message=message,
        line=int(getattr(node, "lineno", 0) or 0),
        column=int(getattr(node, "col_offset", 0) or 0) + 1 if hasattr(node, "col_offset") else 0,
    )


class _SafetyVisitor(ast.NodeVisitor):
    _forbidden = (
        ast.AsyncFunctionDef, ast.Await, ast.ClassDef, ast.Delete, ast.Global,
        ast.Nonlocal, ast.While, ast.With, ast.Yield, ast.YieldFrom,
    )

    def __init__(self, root: ast.FunctionDef) -> None:
        self.root = root
        self.diagnostics: list[CodeDiagnostic] = []
        self.assertions: list[ast.Assert] = []
        self.local_functions: set[str] = set()
        self.safe_module_names: set[str] = set()
        self.safe_symbol_names: set[str] = set()

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, self._forbidden):
            self.diagnostics.append(_diag(
                node, "UNSAFE_SYNTAX", f"{type(node).__name__} is not allowed",
            ))
            return
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node is not self.root:
            if node.decorator_list:
                self.diagnostics.append(_diag(
                    node, "UNSAFE_FUNCTION", "local helpers cannot use decorators",
                ))
                return
            self.local_functions.add(node.name)
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    self.visit(default)
            for statement in node.body:
                self.visit(statement)
            return
        for statement in node.body:
            self.visit(statement)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id.startswith("__"):
            self.diagnostics.append(_diag(node, "DUNDER_ACCESS", "dunder names are not allowed"))

    def visit_Assert(self, node: ast.Assert) -> None:  # noqa: N802
        self.assertions.append(node)
        if isinstance(node.test, ast.Constant):
            self.diagnostics.append(_diag(
                node,
                "BUSINESS_ASSERTION_CONSTANT",
                "business assertions must check runtime-derived state",
            ))
        if node.msg is None or (
            isinstance(node.msg, ast.Constant)
            and not str(node.msg.value or "").strip()
        ):
            self.diagnostics.append(_diag(
                node,
                "BUSINESS_ASSERTION_MESSAGE",
                "business assertions need a diagnostic message",
            ))
        self.visit(node.test)
        if node.msg is not None:
            self.visit(node.msg)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name not in SAFE_MODULES:
                self.diagnostics.append(_diag(
                    node, "UNSAFE_IMPORT", f"module {alias.name!r} is not allowed",
                ))
                continue
            self.safe_module_names.add(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if node.level or node.module not in SAFE_MODULES:
            self.diagnostics.append(_diag(
                node, "UNSAFE_IMPORT", f"module {node.module!r} is not allowed",
            ))
            return
        module = SAFE_MODULES[node.module]
        for alias in node.names:
            if alias.name == "*" or not hasattr(module, alias.name):
                self.diagnostics.append(_diag(
                    node, "UNSAFE_IMPORT", f"symbol {alias.name!r} is not allowed",
                ))
                continue
            imported_name = alias.asname or alias.name
            self.safe_module_names.add(imported_name)
            self.safe_symbol_names.add(imported_name)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr.startswith("__"):
            self.diagnostics.append(_diag(node, "DUNDER_ACCESS", "dunder attributes are not allowed"))
            return
        if isinstance(node.value, ast.Name) and node.value.id == "ctx":
            if node.attr not in CTX_METHODS:
                self.diagnostics.append(_diag(
                    node, "UNKNOWN_CTX_API", f"ctx.{node.attr} is not an allowed capability",
                ))
            return
        root = node.value
        while isinstance(root, ast.Attribute):
            root = root.value
        from_safe_module = isinstance(root, ast.Name) and root.id in self.safe_module_names
        if not from_safe_module and node.attr not in SAFE_METHODS | SAFE_PROPERTIES:
            self.diagnostics.append(_diag(
                node, "UNSAFE_ATTRIBUTE", f"attribute method {node.attr!r} is not allowed",
            ))
            return
        self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        ctx_method = None
        if isinstance(node.func, ast.Name):
            if (
                node.func.id not in SAFE_BUILTINS
                and node.func.id not in self.local_functions
                and node.func.id not in self.safe_symbol_names
            ):
                self.diagnostics.append(_diag(
                    node, "UNSAFE_CALL", f"function {node.func.id!r} is not allowed",
                ))
        elif isinstance(node.func, ast.Attribute):
            self.visit(node.func)
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx":
                ctx_method = node.func.attr
        else:
            self.diagnostics.append(_diag(node, "UNSAFE_CALL", "dynamic calls are not allowed"))
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            if keyword.arg is None:
                self.diagnostics.append(_diag(keyword, "KWARGS_EXPANSION", "**kwargs is not allowed"))
            self.visit(keyword.value)
        if ctx_method in CTX_METHODS:
            self._check_ctx_signature(node, ctx_method)

    def _check_ctx_signature(self, node: ast.Call, method: str) -> None:
        signatures = {
            "lookup": ({"entity"}, {"entity", "field", "fallback"}, 1),
            "acquire": ({"scope", "fields"}, {"scope", "fields", "coverage"}, 1),
            "read": ({"fields"}, {"target", "fields"}, 1),
            "interact": (
                {"goal", "success"},
                {
                    "goal",
                    "success",
                    "inputs",
                    "required_values",
                    "observe_fields",
                    "persistence",
                },
                1,
            ),
            "command": ({"capability"}, None, 1),
            "compute": ({"operation"}, None, 1),
        }
        required, allowed, positional_slots = signatures[method]
        keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
        supplied = set(keyword_names)
        positional_names = {
            "lookup": ["entity", "field", "fallback"],
            "acquire": ["scope", "fields", "coverage"],
            "read": ["target", "fields"],
            "interact": [
                "goal",
                "success",
                "inputs",
                "required_values",
                "observe_fields",
                "persistence",
            ],
            "command": ["capability"],
            "compute": ["operation"],
        }[method]
        positional_supplied = set(positional_names[:len(node.args)])
        supplied.update(positional_supplied)
        duplicated = sorted(positional_supplied & keyword_names)
        if duplicated:
            self.diagnostics.append(_diag(
                node,
                "CTX_SIGNATURE",
                f"ctx.{method} receives multiple values for arguments {duplicated}",
            ))
        missing = sorted(required - supplied)
        if missing:
            self.diagnostics.append(_diag(
                node,
                "CTX_SIGNATURE",
                f"ctx.{method} is missing required arguments {missing}",
            ))
        if len(node.args) > positional_slots:
            self.diagnostics.append(_diag(
                node,
                "CTX_SIGNATURE",
                f"ctx.{method} accepts at most {positional_slots} positional argument",
            ))
        if allowed is not None:
            unexpected = sorted(name for name in keyword_names if name not in allowed)
            if unexpected:
                self.diagnostics.append(_diag(
                    node,
                    "CTX_SIGNATURE",
                    f"ctx.{method} has unexpected arguments {unexpected}",
                ))
        if method == "acquire":
            scope = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "scope"),
                node.args[0] if node.args else None,
            )
            if isinstance(scope, ast.Constant) and scope.value is None:
                self.diagnostics.append(_diag(
                    node,
                    "ACQUIRE_SCOPE_REQUIRED",
                    "ctx.acquire requires a LookupScope from ctx.lookup; current-view None is invalid",
                ))
        if method == "interact":
            self._check_interact_contract(node)

    def _check_interact_contract(self, node: ast.Call) -> None:
        keywords = {
            keyword.arg: keyword.value
            for keyword in node.keywords
            if keyword.arg is not None
        }
        success = keywords.get("success")
        if (
            isinstance(success, ast.Constant)
            and not isinstance(success.value, str)
        ):
            self.diagnostics.append(_diag(
                success,
                "CTX_SIGNATURE",
                "ctx.interact success must be a verifiable string postcondition",
            ))
        persistence = keywords.get("persistence")
        if not isinstance(persistence, ast.Constant) or not isinstance(persistence.value, str):
            return
        if persistence.value not in {"immediate", "explicit_commit"}:
            self.diagnostics.append(_diag(
                persistence,
                "INTERACT_PERSISTENCE",
                "persistence must be 'immediate' or 'explicit_commit'",
            ))
            return
        if persistence.value == "explicit_commit":
            required_values = keywords.get("required_values")
            if (
                required_values is None
                or isinstance(required_values, ast.Dict) and not required_values.keys
                or isinstance(required_values, ast.Constant) and required_values.value is None
            ):
                self.diagnostics.append(_diag(
                    node,
                    "INTERACT_REQUIRED_VALUES",
                    (
                        "durable ctx.interact must declare nonempty required_values containing "
                        "the business values that the Statement must write and verify"
                    ),
                ))
        for argument_name in ("inputs", "required_values"):
            argument = keywords.get(argument_name)
            if argument is None:
                continue
            non_json = next(
                (
                    child
                    for child in ast.walk(argument)
                    if isinstance(child, (ast.Set, ast.SetComp))
                    or (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id == "set"
                    )
                ),
                None,
            )
            if non_json is not None:
                self.diagnostics.append(_diag(
                    non_json,
                    "INTERACT_JSON_VALUE",
                    (
                        f"ctx.interact {argument_name} must contain JSON values; "
                        "replace set values with deterministic lists"
                    ),
                ))


def _ctx_acquire_assignment(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
        and node.func.attr == "acquire"
    )


def _lookup_entity_diagnostics(function: ast.FunctionDef) -> list[CodeDiagnostic]:
    scope_vars = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(target := node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "ctx"
        and node.value.func.attr == "lookup"
    }
    diagnostics: list[CodeDiagnostic] = []
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
            and node.func.attr == "lookup"
        ):
            continue
        entity = (
            node.args[0]
            if node.args
            else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "entity"
                ),
                None,
            )
        )
        if isinstance(entity, ast.Name) and entity.id in scope_vars:
            diagnostics.append(_diag(
                entity,
                "LOOKUP_ENTITY_REQUIRED",
                (
                    "ctx.lookup entity must be a textual business mention, not a LookupScope; "
                    "reuse the existing scope in ctx.acquire or start a new lookup from text"
                ),
            ))
    return diagnostics


def _business_identity_break_diagnostics(
    function: ast.FunctionDef,
) -> list[CodeDiagnostic]:
    collection_vars: set[str] = set()
    dependencies: dict[str, set[str]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if _ctx_acquire_assignment(node.value):
            collection_vars.add(target.id)
        dependencies[target.id] = _loaded_names(node.value)
    changed = True
    while changed:
        changed = False
        for target, sources in dependencies.items():
            if target not in collection_vars and sources & collection_vars:
                collection_vars.add(target)
                changed = True

    parents = {
        child: parent
        for parent in ast.walk(function)
        for child in ast.iter_child_nodes(parent)
    }
    diagnostics: list[CodeDiagnostic] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Break):
            continue
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, (ast.For, ast.FunctionDef)):
            parent = parents.get(parent)
        if (
            isinstance(parent, ast.For)
            and _loaded_names(parent.iter) & collection_vars
        ):
            diagnostics.append(_diag(
                node,
                "BUSINESS_IDENTITY_FIRST_MATCH",
                (
                    "do not break on the first record from an acquired business collection; "
                    "collect all qualifying candidates, assert cardinality, then select"
                ),
            ))
    return diagnostics


def _date_constructor_diagnostics(function: ast.FunctionDef) -> list[CodeDiagnostic]:
    diagnostics: list[CodeDiagnostic] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        function_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if function_name not in {"date", "datetime"}:
            continue
        day = (
            node.args[2]
            if len(node.args) >= 3
            else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "day"
                ),
                None,
            )
        )
        if (
            isinstance(day, ast.Constant)
            and isinstance(day.value, int)
            and not 1 <= day.value <= 31
        ):
            diagnostics.append(_diag(
                day,
                "INVALID_DATE_CONSTRUCTION",
                (
                    f"{function_name} day must be between 1 and 31; derive month boundaries "
                    "with the first day of a month plus or minus timedelta"
                ),
            ))
    return diagnostics


def _durable_interact_calls(function: ast.FunctionDef) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
            and node.func.attr == "interact"
        ):
            continue
        persistence = next(
            (
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "persistence"
            ),
            None,
        )
        if (
            isinstance(persistence, ast.Constant)
            and persistence.value == "explicit_commit"
        ):
            calls.append(node)
    return calls


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def _interact_result_diagnostics(function: ast.FunctionDef) -> list[CodeDiagnostic]:
    parents = {
        child: parent
        for parent in ast.walk(function)
        for child in ast.iter_child_nodes(parent)
    }
    diagnostics: list[CodeDiagnostic] = []
    for call in _durable_interact_calls(function):
        node: ast.AST = call
        assigned_names: set[str] = set()
        consumed = False
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.Assert, ast.If, ast.Return)):
                consumed = True
                break
            if isinstance(node, ast.Assign):
                assigned_names = {
                    target.id
                    for target in node.targets
                    if isinstance(target, ast.Name)
                }
                break
            if isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    assigned_names = {node.target.id}
                break
            if isinstance(node, (ast.Expr, ast.FunctionDef)):
                break
        if assigned_names:
            for consumer in ast.walk(function):
                if (
                    isinstance(consumer, (ast.Assert, ast.If, ast.Return))
                    and int(getattr(consumer, "lineno", 0) or 0) > call.lineno
                    and assigned_names & _loaded_names(consumer)
                ):
                    consumed = True
                    break
        if not consumed:
            diagnostics.append(_diag(
                call,
                "INTERACT_RESULT_UNUSED",
                (
                    "capture the boolean result of durable ctx.interact and assert, branch, "
                    "or return on it so a failed Statement cannot be reported as success"
                ),
            ))
    return diagnostics


def validate_code(
    source: str,
    *,
    require_business_assertions: bool = True,
) -> list[CodeDiagnostic]:
    try:
        tree = ast.parse(source, filename="<coding-plan>")
    except SyntaxError as exc:
        return [CodeDiagnostic(
            code="SYNTAX_ERROR",
            message=exc.msg,
            line=int(exc.lineno or 0),
            column=int(exc.offset or 0),
        )]
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if (
        len(functions) != 1
        or any(not isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom)) for node in tree.body)
    ):
        return [CodeDiagnostic(
            "ENTRYPOINT",
            "source must contain only safe imports followed by exactly one def run(ctx)",
        )]
    function = functions[0]
    args = function.args
    if (
        function.name != "run"
        or [argument.arg for argument in args.args] != ["ctx"]
        or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg
        or function.decorator_list
    ):
        return [_diag(function, "ENTRYPOINT", "entrypoint must be exactly def run(ctx)")]
    visitor = _SafetyVisitor(function)
    for node in tree.body:
        visitor.visit(node)
    if require_business_assertions and not visitor.assertions:
        visitor.diagnostics.append(_diag(
            function,
            "BUSINESS_ASSERTION_REQUIRED",
            "program must contain at least one runtime-derived business assertion",
        ))
    visitor.diagnostics.extend(_business_identity_break_diagnostics(function))
    visitor.diagnostics.extend(_date_constructor_diagnostics(function))
    visitor.diagnostics.extend(_lookup_entity_diagnostics(function))
    visitor.diagnostics.extend(_interact_result_diagnostics(function))
    return visitor.diagnostics


def _lookup_key(value: str) -> str:
    return value.strip().casefold()


def _lookup_rows(
    lookups: dict[str, list[dict[str, Any]]],
    value: str,
) -> list[dict[str, Any]] | None:
    exact = lookups.get(_lookup_key(value))
    if exact is not None:
        return exact
    digits = re.sub(r"\D+", "", value)
    if len(digits) < 7:
        return None
    matches = [
        rows
        for alias, rows in lookups.items()
        if (
            len(alias_digits := re.sub(r"\D+", "", alias)) >= 7
            and (digits.endswith(alias_digits) or alias_digits.endswith(digits))
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _semantic_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _literal_fields(node: ast.Call) -> list[str] | None:
    fields_node = next(
        (
            keyword.value
            for keyword in node.keywords
            if keyword.arg == "fields"
        ),
        None,
    )
    if not isinstance(fields_node, (ast.List, ast.Tuple)):
        return None
    fields: list[str] = []
    for element in fields_node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        fields.append(element.value)
    return fields


def validate_fixture_contract(
    source: str,
    fixture: FixtureSpec,
) -> list[CodeDiagnostic]:
    """Report literal fields that no supplied mock source can provide."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    collection_fields = {
        _semantic_key(str(field_name))
        for rows in fixture.lookups.values()
        for row in rows
        for field_name in row
    }
    detail_fields = collection_fields | {
        _semantic_key(str(field_name))
        for state in fixture.reads.values()
        for field_name in state
    }
    diagnostics: list[CodeDiagnostic] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
            and node.func.attr in {"acquire", "read"}
        ):
            continue
        requested = _literal_fields(node)
        if requested is None:
            continue
        available = collection_fields if node.func.attr == "acquire" else detail_fields
        if not available:
            continue
        missing = [
            field_name
            for field_name in requested
            if _semantic_key(field_name) not in available
        ]
        if missing:
            diagnostics.append(_diag(
                node,
                "MOCK_FIELD_UNAVAILABLE",
                (
                    f"ctx.{node.func.attr} fields {missing!r} are unavailable from every "
                    f"supplied mock {node.func.attr} source"
                ),
            ))
    return diagnostics


class _ProjectionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.mapping_fields: dict[str, set[str]] = {}
        self.sequence_fields: dict[str, set[str]] = {}
        self.diagnostics: list[CodeDiagnostic] = []

    @staticmethod
    def _ctx_fields(node: ast.AST, method: str) -> set[str] | None:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
            and node.func.attr == method
        ):
            return None
        fields = _literal_fields(node)
        if fields is None:
            return None
        return {_semantic_key(field_name) for field_name in fields}

    def _infer_mapping(self, node: ast.AST) -> set[str] | None:
        read_fields = self._ctx_fields(node, "read")
        if read_fields is not None:
            return read_fields
        if isinstance(node, ast.Name):
            return self.mapping_fields.get(node.id)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
        ):
            return self.sequence_fields.get(node.value.id)
        return None

    def _infer_sequence(self, node: ast.AST) -> set[str] | None:
        acquire_fields = self._ctx_fields(node, "acquire")
        if acquire_fields is not None:
            return acquire_fields
        if isinstance(node, ast.Name):
            return self.sequence_fields.get(node.id)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"list", "sorted"}
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            return self.sequence_fields.get(node.args[0].id)
        if (
            isinstance(node, ast.ListComp)
            and len(node.generators) == 1
            and isinstance(node.elt, ast.Name)
            and isinstance(node.generators[0].target, ast.Name)
            and node.elt.id == node.generators[0].target.id
        ):
            generator = node.generators[0]
            if isinstance(generator.iter, ast.Name):
                return self.sequence_fields.get(generator.iter.id)
        return None

    def _bind(
        self,
        target: ast.AST,
        *,
        mapping: set[str] | None = None,
        sequence: set[str] | None = None,
    ) -> None:
        if not isinstance(target, ast.Name):
            return
        self.mapping_fields.pop(target.id, None)
        self.sequence_fields.pop(target.id, None)
        if mapping is not None:
            self.mapping_fields[target.id] = set(mapping)
        if sequence is not None:
            self.sequence_fields[target.id] = set(sequence)

    def _check_field(self, node: ast.AST, variable: str, field_name: str) -> None:
        available = self.mapping_fields.get(variable)
        if available is None or _semantic_key(field_name) in available:
            return
        self.diagnostics.append(_diag(
            node,
            "PROJECTED_FIELD_UNAVAILABLE",
            (
                f"{variable!r} accesses field {field_name!r}, but its projected fields are "
                f"{sorted(available)!r}"
            ),
        ))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        mapping = self._infer_mapping(node.value)
        sequence = self._infer_sequence(node.value)
        for target in node.targets:
            self._bind(target, mapping=mapping, sequence=sequence)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._bind(
                node.target,
                mapping=self._infer_mapping(node.value),
                sequence=self._infer_sequence(node.value),
            )

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        fields = (
            self.sequence_fields.get(node.iter.id)
            if isinstance(node.iter, ast.Name)
            else None
        )
        self._bind(node.target, mapping=fields)
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        saved_mappings = dict(self.mapping_fields)
        for generator in node.generators:
            self.visit(generator.iter)
            fields = (
                self.sequence_fields.get(generator.iter.id)
                if isinstance(generator.iter, ast.Name)
                else None
            )
            self._bind(generator.target, mapping=fields)
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)
        self.mapping_fields = saved_mappings

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self._check_field(node, node.func.value.id, node.args[0].value)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if (
            isinstance(node.value, ast.Name)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            self._check_field(node, node.value.id, node.slice.value)
        elif (
            isinstance(node.value, ast.Subscript)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in self.sequence_fields
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            available = self.sequence_fields[node.value.value.id]
            if _semantic_key(node.slice.value) not in available:
                self.diagnostics.append(_diag(
                    node,
                    "PROJECTED_FIELD_UNAVAILABLE",
                    (
                        f"record from {node.value.value.id!r} accesses field "
                        f"{node.slice.value!r}, but its projected fields are "
                        f"{sorted(available)!r}"
                    ),
                ))
        self.generic_visit(node)


def validate_projection_contract(source: str) -> list[CodeDiagnostic]:
    """Check field access against literal fields projected by acquire/read."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = _ProjectionVisitor()
    visitor.visit(tree)
    return visitor.diagnostics


def validate_runtime_dataflow(source: str) -> list[CodeDiagnostic]:
    """Report values derived from ctx.read that are assigned but never consumed."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    assignments: dict[str, ast.Assign | ast.AnnAssign] = {}
    dependencies: dict[str, set[str]] = {}
    direct_read_vars: set[str] = set()
    loads: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads[node.id] = loads.get(node.id, 0) + 1
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
            value = node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        assignments[target.id] = node
        dependencies[target.id] = {
            name.id
            for name in ast.walk(value)
            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)
        }
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "ctx"
            and value.func.attr == "read"
        ):
            direct_read_vars.add(target.id)
    runtime_vars = set(direct_read_vars)
    changed = True
    while changed:
        changed = False
        for target, sources in dependencies.items():
            if target not in runtime_vars and sources & runtime_vars:
                runtime_vars.add(target)
                changed = True
    diagnostics: list[CodeDiagnostic] = []
    for variable in sorted(runtime_vars):
        if loads.get(variable, 0) > 0:
            continue
        node = assignments[variable]
        diagnostics.append(_diag(
            node,
            "UNUSED_RUNTIME_VALUE",
            (
                f"runtime-derived value {variable!r} is assigned but never used; when the task "
                "requests a relative change, consume it in the requested calculation and pass "
                "the result to required_values; otherwise delete the entire unnecessary read "
                "assignment—leaving the assignment unchanged never fixes this diagnostic"
            ),
        ))
    return diagnostics


def _field_value(mapping: dict[str, Any], field_name: str) -> tuple[bool, Any]:
    if field_name in mapping:
        return True, mapping[field_name]
    folded = _semantic_key(field_name)
    for key, value in mapping.items():
        if _semantic_key(str(key)) == folded:
            return True, value
    return False, None


def _target_key(target: Any) -> str:
    if isinstance(target, dict):
        for field_name in (
            "id", "ID", "order_id", "increment_id", "action_url", "Action_url", "Action",
            "sku", "SKU", "name", "Name", "title", "Title", "key",
        ):
            if field_name in target and target[field_name] is not None:
                return str(target[field_name])
    return str(target)


class _FixtureContext:
    def __init__(self, fixture: FixtureSpec) -> None:
        self.fixture = fixture
        self.trace: list[TraceEvent] = []
        self.writes: list[WriteEvent] = []
        self.current_target: Any = None
        self.read_aliases: dict[str, str] = {}
        self.state: dict[str, dict[str, Any]] = {}
        for rows in fixture.lookups.values():
            for row in rows:
                canonical = str(row.get("id") or row.get("ID") or "")
                if not canonical:
                    continue
                self.state.setdefault(canonical, {}).update(copy.deepcopy(row))
                for field_name in (
                    "id", "ID", "action_url", "Action_url", "Action", "sku", "SKU", "name", "Name",
                    "title", "Title", "key",
                ):
                    value = row.get(field_name)
                    if value is not None:
                        self.read_aliases[str(value)] = canonical
        for target_id, detail in fixture.reads.items():
            canonical = self.read_aliases.get(str(target_id), str(target_id))
            self.state.setdefault(canonical, {}).update(copy.deepcopy(detail))
            self.read_aliases[str(target_id)] = canonical
        for canonical, state in self.state.items():
            for field_name in (
                "id", "ID", "order_id", "increment_id", "action_url", "Action_url", "Action",
                "sku", "SKU", "name", "Name", "title", "Title", "key",
            ):
                value = state.get(field_name)
                if value is not None:
                    self.read_aliases[str(value)] = canonical

    def lookup(
        self,
        entity: str,
        field: str = "name",
        fallback: str | None = None,
    ) -> LookupScope:
        scope = LookupScope(entity=entity, field=field, fallback=fallback or "")
        self.trace.append(TraceEvent("lookup", (entity,), {"field": field, "fallback": fallback}, scope))
        return scope

    def acquire(
        self,
        scope: LookupScope,
        fields: list[str],
        coverage: str = "complete",
    ) -> list[dict[str, Any]]:
        rows = _lookup_rows(self.fixture.lookups, scope.entity)
        if rows is None and scope.fallback:
            rows = _lookup_rows(self.fixture.lookups, scope.fallback)
        if rows is None:
            rows = []
        missing = [
            name for name in fields
            if rows and not any(_field_value(row, name)[0] for row in rows)
        ]
        if missing:
            available_fields = sorted({
                str(field_name)
                for row in rows
                for field_name in row
            })
            raise KeyError(
                f"collection fields {missing!r} are unavailable for scope {scope.entity!r}; "
                f"available_fields={available_fields!r}"
            )
        projected = [
            {name: _field_value(row, name)[1] for name in fields}
            for row in rows
        ]
        self.trace.append(TraceEvent(
            "acquire", (scope,), {"fields": list(fields), "coverage": coverage}, projected,
        ))
        return projected

    def read(self, target: Any = None, fields: list[str] | None = None) -> dict[str, Any]:
        if fields is None:
            raise TypeError("ctx.read requires fields")
        actual_target = self.current_target if target is None else target
        key = _target_key(actual_target)
        key = self.read_aliases.get(key, key)
        if key not in self.state:
            raise KeyError(f"no fixture read state for target {key!r}")
        state = self.state[key]
        missing = [name for name in fields if not _field_value(state, name)[0]]
        if missing:
            raise KeyError(f"fixture target {key!r} has no fields {missing}")
        result = {name: _field_value(state, name)[1] for name in fields}
        self.trace.append(TraceEvent("read", (actual_target,), {"fields": list(fields)}, result))
        return result

    def _state_key_for_input(self, value: Any) -> str | None:
        if not isinstance(value, dict) or not value:
            return None
        direct_key = self.read_aliases.get(_target_key(value))
        if direct_key is not None:
            return direct_key
        matches: list[str] = []
        for key, state in self.state.items():
            if all(
                _field_value(state, str(field_name))[0]
                and _field_value(state, str(field_name))[1] == field_value
                for field_name, field_value in value.items()
            ):
                matches.append(key)
        return matches[0] if len(matches) == 1 else None

    def _statement_target(
        self,
        inputs: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        resolved_by_key: dict[str, tuple[dict[str, Any], str]] = {}
        for value in inputs.values():
            key = (
                self._state_key_for_input(value)
                if isinstance(value, dict)
                else self.read_aliases.get(str(value))
                if isinstance(value, (str, int, float)) and not isinstance(value, bool)
                else None
            )
            if key is not None and key in self.state:
                resolved_by_key[key] = (self.state[key], key)
        resolved = list(resolved_by_key.values())
        if len(resolved) == 1:
            return resolved[0]
        return None, None

    def interact(
        self,
        goal: str,
        *,
        success: str,
        inputs: dict[str, Any] | None = None,
        required_values: dict[str, Any] | None = None,
        observe_fields: list[str] | None = None,
        persistence: str = "immediate",
    ) -> bool:
        contract = StatementContract(
            id=f"mock-s{len(self.trace) + 1}",
            goal=goal,
            success=success,
            inputs=copy.deepcopy(inputs or {}),
            required_values=copy.deepcopy(required_values or {}),
            observe_fields=list(observe_fields or []),
            persistence=persistence,
        )
        statement_inputs = dict(contract.inputs)
        desired_values = dict(contract.required_values)
        target, key = self._statement_target(statement_inputs)
        if target is not None:
            self.current_target = target
        write: WriteEvent | None = None
        if key is not None and desired_values and contract.persistence == "explicit_commit":
            state = self.state.get(key)
            before = copy.deepcopy(state) if state is not None else {}
            if state is not None:
                for field_name, value in desired_values.items():
                    existing = next(
                        (
                            key_name for key_name in state
                            if _semantic_key(str(key_name)) == _semantic_key(str(field_name))
                        ),
                        field_name,
                    )
                    state[existing] = copy.deepcopy(value)
            write = WriteEvent(
                goal=contract.goal,
                success=contract.success,
                target_id=key,
                inputs=copy.deepcopy(statement_inputs),
                required_values=copy.deepcopy(desired_values),
                observe_fields=list(contract.observe_fields),
                persistence=contract.persistence,
                before=before,
                after=copy.deepcopy(state) if state is not None else {},
                applied=True,
            )
        elif contract.persistence == "explicit_commit":
            write = WriteEvent(
                goal=contract.goal,
                success=contract.success,
                target_id=key,
                inputs=copy.deepcopy(statement_inputs),
                required_values=copy.deepcopy(desired_values),
                observe_fields=list(contract.observe_fields),
                persistence=contract.persistence,
                after=copy.deepcopy(desired_values),
                applied=True,
            )
        if write is not None:
            self.writes.append(write)
        result = True
        self.trace.append(TraceEvent("interact", (goal,), {
            "success": contract.success,
            "inputs": statement_inputs,
            "required_values": desired_values,
            "observe_fields": list(contract.observe_fields),
            "persistence": contract.persistence,
        }, result))
        return result

    def command(self, capability: str, **kwargs: Any) -> Any:
        result = self.fixture.command_results.get(capability)
        self.trace.append(TraceEvent("command", (capability,), dict(kwargs), result))
        return result

    def compute(self, operation: str, **inputs: Any) -> Any:
        if operation not in self.fixture.compute_results:
            raise KeyError(f"no fixture compute result for operation {operation!r}")
        result = self.fixture.compute_results[operation]
        self.trace.append(TraceEvent("compute", (operation,), dict(inputs), result))
        return result


def _worker(source: str, fixture: FixtureSpec, output: Any) -> None:
    ctx = _FixtureContext(fixture)
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "__name__": "coding_plan",
    }
    try:
        exec(compile(source, "<coding-plan>", "exec"), namespace, namespace)
        result = namespace["run"](ctx)
        output.put({
            "ok": True,
            "return_value": result,
            "trace": ctx.trace,
            "writes": ctx.writes,
            "final_state": ctx.state,
        })
    except BaseException:  # noqa: BLE001 - child must serialize any plan failure
        output.put({
            "ok": False,
            "error": traceback.format_exc(limit=8),
            "trace": ctx.trace,
            "writes": ctx.writes,
            "final_state": ctx.state,
        })


def execute_code(source: str, fixture: FixtureSpec, *, timeout: float = 5.0) -> CodingRunResult:
    diagnostics = validate_code(source)
    if diagnostics:
        return CodingRunResult(
            ok=False,
            error="\n".join(diagnostic.render() for diagnostic in diagnostics),
        )
    mp = multiprocessing.get_context("spawn")
    output = mp.Queue()
    process = mp.Process(target=_worker, args=(source, fixture, output), daemon=True)
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
        return CodingRunResult(ok=False, error=f"execution exceeded {timeout:.1f}s", timed_out=True)
    try:
        payload = output.get(timeout=0.2)
    except queue.Empty:
        return CodingRunResult(
            ok=False,
            error=f"sandbox exited without a result (exitcode={process.exitcode})",
        )
    return CodingRunResult(
        ok=bool(payload.get("ok")),
        return_value=payload.get("return_value"),
        trace=list(payload.get("trace") or []),
        writes=list(payload.get("writes") or []),
        final_state=dict(payload.get("final_state") or {}),
        error=str(payload.get("error") or ""),
    )


__all__ = [
    "CTX_METHODS",
    "FixtureSpec",
    "LookupScope",
    "execute_code",
    "validate_code",
    "validate_fixture_contract",
    "validate_projection_contract",
    "validate_runtime_dataflow",
]
