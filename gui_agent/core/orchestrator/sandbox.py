"""Restricted source validation and fixture execution for coding plans.

This is an experiment boundary, not a production-grade hostile-code sandbox.
The child process, AST allow-list and timeout keep evaluation failures isolated.
"""

from __future__ import annotations

import ast
import copy
import datetime
import __future__
import math
import multiprocessing
import queue
import re
import symtable
import time as _time
import traceback
import typing
import _strptime
from dataclasses import dataclass, field
from typing import Any

from gui_agent.core.run.statements.compute_kernel import json_value, normalize_table_rows
from gui_agent.core.filter_contract import canonical_filter_value

from .models import (
    CodeDiagnostic,
    CodingRunResult,
    TraceEvent,
    CurrentUI,
    WriteEvent,
    field_projection,
    reach_postcondition,
    require_current_ui,
)


CTX_METHODS = frozenset({"reach", "query", "read", "commit", "command"})
CTX_SIGNATURES = {
    "query": (
        ("entity", "fields", "filters", "coverage"),
        {"entity", "fields"},
    ),
    "read": (("target", "fields"), {"fields"}),
    "reach": (("goal", "success", "target"), {"goal", "success"}),
    "commit": (("goal", "target", "values"), {"goal", "values"}),
    "command": (("capability",), {"capability"}),
}
SAFE_MODULES = {
    "__future__": __future__, "datetime": datetime, "math": math, "typing": typing,
}
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
    "AssertionError": AssertionError,
    "Exception": Exception,
    "IndexError": IndexError,
    "KeyError": KeyError,
    "RuntimeError": RuntimeError,
    "TypeError": TypeError,
    "ValueError": ValueError,
    "zip": zip,
}
SAFE_METHODS = frozenset({
    "add", "append", "casefold", "count", "date", "endswith", "extend", "find", "fromisoformat",
    "format", "get", "index", "items", "join", "keys", "lower", "lstrip", "partition",
    "removeprefix", "removesuffix", "replace", "reverse", "rfind", "rpartition",
    "rsplit", "rstrip", "sort", "split", "splitlines", "startswith", "strftime",
    "strip", "strptime", "upper", "values", "zfill",
})
SAFE_PROPERTIES = frozenset({"day", "hour", "microsecond", "minute", "month", "second", "year"})
IDENTITY_FIELDS = (
    "id", "ID", "order_id", "increment_id", "action_url", "Action_url", "Action",
    "sku", "SKU", "name", "Name", "title", "Title", "key",
)
GLOBAL_TARGET_LOCATOR_FIELDS = frozenset({
    "actionurl", "href", "permalink", "url",
})


@dataclass(frozen=True)
class FixtureSpec:
    lookups: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reads: dict[str, dict[str, Any]] = field(default_factory=dict)
    command_results: dict[str, Any] = field(default_factory=dict)

    def fields(self, *, include_reads: bool = False) -> set[str]:
        records = [row for rows in self.lookups.values() for row in rows]
        if include_reads:
            records.extend(self.reads.values())
        return {str(field_name) for record in records for field_name in record}


def _ctx_method(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
    ):
        return node.func.attr
    return None


def _call_argument(node: ast.Call, name: str, position: int) -> ast.AST | None:
    return next(
        (keyword.value for keyword in node.keywords if keyword.arg == name),
        node.args[position] if len(node.args) > position else None,
    )


def _same_expression(left: ast.AST | None, right: ast.AST | None) -> bool:
    return left is not None and right is not None and ast.dump(left) == ast.dump(right)


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
        self.local_functions.update(
            child.name
            for child in ast.walk(node)
            if isinstance(child, ast.FunctionDef) and child is not node
        )
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
        ctx_method = _ctx_method(node)
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
        positional_names, required = CTX_SIGNATURES[method]
        keyword_names = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
        positional_supplied = set(positional_names[:len(node.args)])
        supplied = keyword_names | positional_supplied
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
        max_positional = 0 if method in {"query", "read"} else 1
        if len(node.args) > max_positional:
            self.diagnostics.append(_diag(
                node,
                "CTX_SIGNATURE",
                f"ctx.{method} accepts at most {max_positional} positional arguments",
            ))
        if method != "command":
            unexpected = sorted(keyword_names - set(positional_names))
            if unexpected:
                self.diagnostics.append(_diag(
                    node,
                    "CTX_SIGNATURE",
                    f"ctx.{method} has unexpected arguments {unexpected}",
                ))
        if method in {"reach", "commit"}:
            self._check_world_contract(node, method)
        if method in {"query", "read"}:
            fields = _call_argument(node, "fields", 1)
            if isinstance(fields, (ast.Constant, ast.Dict, ast.List, ast.Tuple)):
                try:
                    field_projection(_literal_value(fields))
                except (TypeError, ValueError):
                    self.diagnostics.append(_diag(
                        fields,
                        "FIELD_PROJECTION_CONTRACT",
                        (
                            f"ctx.{method} fields must be a nonempty name list or "
                            "a mapping using supported value types"
                        ),
                    ))

    def _check_world_contract(self, node: ast.Call, method: str) -> None:
        keywords = {
            keyword.arg: keyword.value
            for keyword in node.keywords
            if keyword.arg is not None
        }
        text_argument_name = "goal"
        task = _call_argument(node, text_argument_name, 0)
        if (
            isinstance(task, ast.Constant) and not isinstance(task.value, str)
            or isinstance(task, (ast.Dict, ast.List, ast.Set, ast.Tuple))
        ):
            self.diagnostics.append(_diag(
                task or node,
                "CTX_SIGNATURE",
                f"ctx.{method} {text_argument_name} must be text",
            ))
        for argument_name in (
            ("target", "values") if method == "commit" else ("success", "target")
        ):
            argument = keywords.get(argument_name)
            if argument is None:
                continue
            non_json = next(
                (
                    child
                    for child in ast.walk(argument)
                    if isinstance(child, (ast.Set, ast.SetComp, ast.Tuple))
                    or (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id in {"set", "tuple"}
                    )
                ),
                None,
            )
            if non_json is not None:
                self.diagnostics.append(_diag(
                    non_json,
                    "CTX_JSON_VALUE",
                    (
                        f"ctx.{method} {argument_name} must contain JSON values; "
                        "replace sets and tuples with deterministic lists"
                    ),
                ))
        success = keywords.get("success")
        if method == "reach" and success is not None:
            success_items = (
                {
                    key.value: value
                    for key, value in zip(success.keys, success.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                if isinstance(success, ast.Dict)
                else {}
            )
            entity = success_items.get("entity")
            fields = success_items.get("fields")
            valid_entity = (
                isinstance(entity, ast.Constant)
                and isinstance(entity.value, str)
                and bool(entity.value.strip())
            )
            valid_fields = (
                fields is None
                or isinstance(fields, ast.List)
                and all(
                    isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and bool(item.value.strip())
                    for item in fields.elts
                )
            )
            if not isinstance(success, ast.Dict) or not valid_entity or not valid_fields:
                self.diagnostics.append(_diag(
                    success,
                    "REACH_SUCCESS_CONTRACT",
                    (
                        "ctx.reach success must be a literal dictionary with "
                        "a nonempty entity and optional fields list of strings; "
                        "preserve entity while adding other observable state keys"
                    ),
                ))
        values = keywords.get("values")
        if method == "commit" and (
            values is None
            or isinstance(values, ast.Dict) and not values.keys
            or isinstance(values, ast.Constant) and values.value is None
        ):
            self.diagnostics.append(_diag(
                node,
                "COMMIT_VALUES_REQUIRED",
                "ctx.commit requires nonempty business values",
            ))

def _literal_value(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return None

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
        day = _call_argument(node, "day", 2)
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


def _undefined_name_diagnostics(
    source: str,
    function: ast.FunctionDef,
) -> list[CodeDiagnostic]:
    module = symtable.symtable(source, "<coding-plan>", "exec")
    run_scope = next(
        (child for child in module.get_children() if child.get_name() == "run"),
        None,
    )
    if run_scope is None:
        return []
    allowed = set(SAFE_BUILTINS) | set(module.get_identifiers())
    scopes = [run_scope]
    for scope in scopes:
        scopes.extend(scope.get_children())
    undefined = {
        name
        for scope in scopes
        for name in scope.get_identifiers()
        if scope.lookup(name).is_referenced()
        and scope.lookup(name).is_global()
        and name not in allowed
    }
    diagnostics: list[CodeDiagnostic] = []
    for name in sorted(undefined):
        node = next(
            (
                item for item in ast.walk(function)
                if isinstance(item, ast.Name)
                and isinstance(item.ctx, ast.Load)
                and item.id == name
            ),
            function,
        )
        diagnostics.append(_diag(
            node,
            "UNDEFINED_NAME",
            f"name {name!r} is read but never defined or safely imported",
        ))
    return diagnostics


def _reach_calls(
    function: ast.FunctionDef,
) -> list[tuple[int, str | None, frozenset[str], ast.Dict]]:
    calls: list[tuple[int, str | None, frozenset[str], ast.Dict]] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _ctx_method(node) != "reach":
            continue
        success_node = _call_argument(node, "success", 1)
        if not isinstance(success_node, ast.Dict):
            continue
        success_items = {
            key.value: value
            for key, value in zip(success_node.keys, success_node.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        entity_value = _literal_value(success_items.get("entity"))
        entity = entity_value if isinstance(entity_value, str) else None
        declared_fields = _literal_value(success_items.get("fields"))
        field_keys = frozenset(
            _semantic_key(field_name)
            for field_name in declared_fields
            if isinstance(field_name, str)
        ) if isinstance(declared_fields, list) else frozenset()
        calls.append((node.lineno, entity, field_keys, success_node))
    return sorted(calls, key=lambda item: item[0])


def _ctx_state_contract_diagnostics(
    function: ast.FunctionDef,
) -> list[CodeDiagnostic]:
    reaches = _reach_calls(function)
    parents = {
        child: parent
        for parent in ast.walk(function)
        for child in ast.iter_child_nodes(parent)
    }

    def control_path(node: ast.AST) -> dict[ast.AST, str]:
        """Return branches/loops that must execute for ``node`` to run."""
        path: dict[ast.AST, str] = {}
        child = node
        while (parent := parents.get(child)) is not None:
            if isinstance(parent, ast.If):
                if child in parent.body:
                    path[parent] = "body"
                elif child in parent.orelse:
                    path[parent] = "orelse"
            elif isinstance(parent, ast.IfExp):
                if child is parent.body:
                    path[parent] = "body"
                elif child is parent.orelse:
                    path[parent] = "orelse"
            elif isinstance(parent, (ast.For, ast.AsyncFor)):
                if child in parent.body:
                    path[parent] = "body"
                elif child in parent.orelse:
                    path[parent] = "orelse"
            child = parent
        return path

    def dominates(source: ast.AST, destination: ast.AST) -> bool:
        source_path = control_path(source)
        destination_path = control_path(destination)
        return all(destination_path.get(owner) == branch for owner, branch in source_path.items())

    def mutually_exclusive(left: ast.AST, right: ast.AST) -> bool:
        left_path = control_path(left)
        right_path = control_path(right)
        return any(
            isinstance(owner, (ast.If, ast.IfExp))
            and owner in right_path
            and right_path[owner] != branch
            for owner, branch in left_path.items()
        )

    def active_reach(node: ast.Call):
        latest = max(
            (
                item
                for item in reaches
                if item[0] < node.lineno and dominates(item[3], node)
            ),
            key=lambda item: item[0],
            default=None,
        )
        if latest is None:
            return None
        invalidated = any(
            isinstance(call, ast.Call)
            and _ctx_method(call) in {"commit", "command"}
            and latest[0] < call.lineno < node.lineno
            and not mutually_exclusive(call, node)
            for call in ast.walk(function)
        )
        return None if invalidated else latest

    def reach_call(item: tuple[int, str | None, frozenset[str], ast.Dict]) -> ast.Call | None:
        owner: ast.AST | None = item[3]
        while owner is not None:
            if isinstance(owner, ast.Call) and _ctx_method(owner) == "reach":
                return owner
            owner = parents.get(owner)
        return None

    def declares_target_identity(
        reach: tuple[int, str | None, frozenset[str], ast.Dict],
        target: ast.AST,
    ) -> bool:
        for key, value in zip(reach[3].keys, reach[3].values):
            if not (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value not in {"entity", "fields"}
            ):
                continue
            if (
                isinstance(value, ast.Subscript)
                and _same_expression(value.value, target)
                and isinstance(value.slice, ast.Constant)
                and value.slice.value == key.value
            ):
                return True
            if isinstance(target, ast.Dict):
                for target_key, target_value in zip(target.keys, target.values):
                    if (
                        isinstance(target_key, ast.Constant)
                        and target_key.value == key.value
                        and _same_expression(value, target_value)
                    ):
                        return True
        return False

    diagnostics: list[CodeDiagnostic] = []
    diagnosed_consumers: set[ast.Call] = set()
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Call) and _ctx_method(value) == "reach":
                diagnostics.append(_diag(
                    node,
                    "REACH_RETURNS_NONE",
                    "ctx.reach updates the one active UI and cannot be assigned",
                ))
            continue
        if isinstance(node, ast.Return) and _ctx_method(node.value) == "commit":
            diagnostics.append(_diag(
                node,
                "RETURN_COMMIT_NONE",
                "ctx.commit returns None; call it as a statement instead of returning it",
            ))
            continue
        if isinstance(node, ast.Return) and _ctx_method(node.value) == "reach":
            diagnostics.append(_diag(
                node,
                "RETURN_REACH_NONE",
                "ctx.reach updates the active UI and returns None; call it as a statement",
            ))
            continue
        if not isinstance(node, ast.Call):
            continue
        method = _ctx_method(node)
        if method == "commit":
            target = _call_argument(node, "target", 1)
            if target is None or (
                isinstance(target, ast.Constant) and target.value is None
            ):
                continue
            reach = active_reach(node)
            active_target = (
                _call_argument(call, "target", 2)
                if reach is not None and (call := reach_call(reach)) is not None
                else None
            )
            if not _same_expression(active_target, target):
                diagnostics.append(_diag(
                    node,
                    "COMMIT_TARGET_UI_REQUIRED",
                    (
                        "targeted ctx.commit requires "
                        "ctx.reach(..., target=<same target expression>, "
                        "success={<identity field>: <same target>[<field>]}) "
                        "after selection and inside the commit loop; do not nest "
                        "the target under success"
                    ),
                ))
            elif reach is not None and not declares_target_identity(reach, target):
                diagnostics.append(_diag(
                    node,
                    "TARGET_REACH_IDENTITY_REQUIRED",
                    (
                        "target-bound ctx.reach success must declare at least one "
                        "exact target identity field and value"
                    ),
                ))
            continue
        if method in {"query", "read"}:
            reach = active_reach(node)
            if reach is None:
                diagnostics.append(_diag(
                    node,
                    "ACTIVE_UI_REQUIRED",
                    f"ctx.{method} requires a preceding active ctx.reach",
                ))
                diagnosed_consumers.add(node)
                continue
            entity = _literal_value(_call_argument(node, "entity", 0))
            if (
                method == "query"
                and reach[1] is not None
                and isinstance(entity, str)
                and reach[1] != entity
            ):
                diagnostics.append(_diag(
                    node,
                    "STATE_ENTITY_MISMATCH",
                    (
                        f"ctx.query entity {entity!r} does not match "
                        f"the active ctx.reach entity {reach[1]!r}"
                    ),
                ))
            if method == "read":
                target = _call_argument(node, "target", 0)
                direct_read = target is None or (
                    isinstance(target, ast.Constant) and target.value is None
                )
                read_fields = _literal_fields(node)
                missing_fields = (
                    [
                        field_name
                        for field_name in read_fields
                        if _semantic_key(field_name) not in reach[2]
                    ]
                    if direct_read and read_fields is not None
                    else []
                )
                if missing_fields:
                    diagnostics.append(_diag(
                        node,
                        "DIRECT_READ_FIELDS_UNDECLARED",
                        (
                            f"direct ctx.read fields {missing_fields!r} are not declared "
                            "by the active ctx.reach; add them to its success.fields list"
                        ),
                    ))

    # A commit or command can invalidate CurrentUI before the next iteration.
    # A consumer earlier in that loop therefore needs a loop-local reach, even
    # when a reach before the loop makes the first iteration valid.
    for loop in (
        node for node in ast.walk(function)
        if isinstance(node, (ast.For, ast.AsyncFor))
    ):
        body_calls = [
            call
            for statement in loop.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call)
        ]
        if not any(_ctx_method(call) in {"commit", "command"} for call in body_calls):
            continue
        loop_reaches = [
            item
            for item in reaches
            if control_path(item[3]).get(loop) == "body"
        ]
        for call in body_calls:
            if (
                call in diagnosed_consumers
                or _ctx_method(call) not in {"query", "read"}
                or any(
                    reach[0] < call.lineno and dominates(reach[3], call)
                    for reach in loop_reaches
                )
            ):
                continue
            diagnostics.append(_diag(
                call,
                "ACTIVE_UI_REQUIRED",
                (
                    f"ctx.{_ctx_method(call)} may run again after the loop invalidates "
                    "CurrentUI; collect reads before commits or reach again inside the loop"
                ),
            ))
            diagnosed_consumers.add(call)
    return diagnostics


def _ctx_effect_lineage_diagnostics(
    function: ast.FunctionDef,
) -> list[CodeDiagnostic]:
    """Validate effects against reach contracts and query-row lineage."""
    Reach = int
    Binding = dict[str, list[tuple[int, set[Reach] | None]]]
    reaches = _reach_calls(function)
    reach_entities = {line: entity for line, entity, _, _ in reaches}
    reach_targets = {
        node.lineno: _call_argument(node, "target", 2)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and _ctx_method(node) == "reach"
    }
    projected: dict[Reach, set[str]] = {}
    sequences: Binding = {}
    rows: Binding = {}

    def latest_reach(line: int) -> Reach | None:
        match = max(
            (item for item in reaches if item[0] < line),
            key=lambda item: item[0],
            default=None,
        )
        return match[0] if match is not None else None

    def lookup(bindings: Binding, name: str, line: int) -> set[Reach] | None:
        return next(
            (
                lineage
                for binding_line, lineage in reversed(bindings.get(name, []))
                if binding_line <= line
            ),
            None,
        )

    def sequence_lineage(node: ast.AST) -> set[Reach] | None:
        if isinstance(node, ast.Call) and _ctx_method(node) == "query":
            reach = latest_reach(node.lineno)
            fields = _literal_fields(node)
            if reach is None or fields is None:
                return None
            projected.setdefault(reach, set()).update(map(_semantic_key, fields))
            return {reach}
        if isinstance(node, ast.Name):
            return lookup(sequences, node.id, node.lineno)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            return sequence_lineage(node.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"list", "sorted"}
            and node.args
        ):
            return sequence_lineage(node.args[0])
        if (
            isinstance(node, ast.ListComp)
            and len(node.generators) == 1
            and isinstance(node.elt, ast.Name)
            and isinstance(node.generators[0].target, ast.Name)
            and node.elt.id == node.generators[0].target.id
        ):
            return sequence_lineage(node.generators[0].iter)
        return None

    def row_lineage(node: ast.AST | None) -> set[Reach] | None:
        if isinstance(node, ast.Name):
            return lookup(rows, node.id, node.lineno)
        if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Slice):
            return sequence_lineage(node.value)
        return None

    def bind(
        target: ast.AST,
        sequence: set[Reach] | None = None,
        row: set[Reach] | None = None,
    ) -> None:
        if not isinstance(target, ast.Name):
            return
        sequences.setdefault(target.id, []).append(
            (target.lineno, set(sequence) if sequence else None)
        )
        rows.setdefault(target.id, []).append(
            (target.lineno, set(row) if row else None)
        )

    nodes = sorted(
        ast.walk(function),
        key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
    )
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, sequence_lineage(node.value), row_lineage(node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bind(node.target, sequence_lineage(node.value), row_lineage(node.value))
        elif isinstance(node, ast.For):
            bind(node.target, row=sequence_lineage(node.iter))
        elif isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            for generator in node.generators:
                bind(generator.target, row=sequence_lineage(generator.iter))
        elif isinstance(node, ast.Call) and _ctx_method(node) == "query":
            sequence_lineage(node)

    predicates: dict[Reach, set[str]] = {}
    commits: dict[Reach, set[str]] = {}
    diagnostics: list[CodeDiagnostic] = []
    predicate_nodes = [
        branch
        for node in nodes
        for branch in (
            [node.test]
            if isinstance(node, (ast.If, ast.While, ast.IfExp))
            else [condition for gen in node.generators for condition in gen.ifs]
            if isinstance(node, (ast.ListComp, ast.GeneratorExp))
            else []
        )
    ]
    for predicate in predicate_nodes:
        for node in ast.walk(predicate):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                owner, field_name = node.value, node.slice.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                owner, field_name = node.func.value, node.args[0].value
            else:
                continue
            if not isinstance(field_name, str):
                continue
            for reach in row_lineage(owner) or ():
                predicates.setdefault(reach, set()).add(_semantic_key(field_name))

    for call in (node for node in nodes if isinstance(node, ast.Call)):
        if _ctx_method(call) != "commit":
            continue
        target = _call_argument(call, "target", 1)
        target_sources = row_lineage(target) or set()
        values = _call_argument(call, "values", 2)
        fields = {
            _semantic_key(key.value)
            for key in values.keys
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
        } if isinstance(values, ast.Dict) else set()
        for reach in target_sources:
            commits.setdefault(reach, set()).update(fields)
        current = latest_reach(call.lineno)
        current_owns_target = (
            current is not None
            and target is not None
            and reach_targets.get(current) is not None
            and _same_expression(reach_targets[current], target)
        )
        inactive_sources = [
            reach
            for reach in target_sources
            if (
                not current_owns_target
                and
                reach != current
                and (
                    reach_entities.get(reach) is None
                    or reach_entities.get(reach) != reach_entities.get(current)
                )
                and projected.get(reach, set()).isdisjoint(
                    GLOBAL_TARGET_LOCATOR_FIELDS
                )
            )
        ]
        if inactive_sources:
            source_lines = ", ".join(map(str, sorted(inactive_sources)))
            diagnostics.append(_diag(
                target or call,
                "COMMIT_TARGET_SOURCE_INACTIVE",
                (
                    "ctx.commit target comes from CurrentUI established by "
                    f"ctx.reach at line(s) {source_lines}, but a later ctx.reach "
                    "activated a different collection; collect reference data first "
                    "and make the target-owning collection current before committing, "
                    "or project a stable global locator such as action_url, url, "
                    "permalink, or href"
                ),
            ))

    for line, _, _, success in reaches:
        for key, value in zip(success.keys, success.values):
            if not (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value not in {"entity", "fields"}
            ):
                continue
            field = _semantic_key(key.value)
            if field in commits.get(line, set()):
                code = "PREMATURE_MUTATION_POSTCONDITION"
                detail = (
                    "later written by ctx.commit on a row from the same query; "
                    "keep durable effects in commit"
                )
            elif (
                field in projected.get(line, set())
                and field in predicates.get(line, set())
            ):
                code = "ROW_FIELD_LEAKED_INTO_REACH"
                detail = (
                    "later read as a predicate on a row from the same query; "
                    "keep row facts in query and Python"
                )
            else:
                continue
            diagnostics.append(_diag(
                value,
                code,
                f"reach success field {key.value!r} is {detail}",
            ))
    return diagnostics


def repair_direct_read_fields(source: str) -> str | None:
    """Complete literal reach field contracts from dependent direct reads.

    The repair is intentionally narrow: it only adds literal field names to the
    literal ``success["fields"]`` list of the latest ``ctx.reach`` call.
    It never changes targets, operations, business values, or returned data.
    """
    try:
        tree = ast.parse(source, filename="<coding-plan>")
    except SyntaxError:
        return None
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    ]
    if len(functions) != 1:
        return None
    function = functions[0]
    reaches = _reach_calls(function)

    changed = False
    calls = sorted(
        (
            node for node in ast.walk(function)
            if isinstance(node, ast.Call) and _ctx_method(node) == "read"
        ),
        key=lambda node: node.lineno,
    )
    for call in calls:
        target = _call_argument(call, "target", 0)
        direct_read = target is None or (
            isinstance(target, ast.Constant) and target.value is None
        )
        fields = _literal_fields(call)
        if not direct_read or fields is None:
            continue
        reach = max(
            (item for item in reaches if item[0] < call.lineno),
            key=lambda item: item[0],
            default=None,
        )
        if reach is None:
            continue
        success = reach[3]
        fields_index = next(
            (
                index
                for index, key in enumerate(success.keys)
                if isinstance(key, ast.Constant) and key.value == "fields"
            ),
            None,
        )
        if fields_index is None:
            success.keys.append(ast.Constant(value="fields"))
            success.values.append(ast.List(elts=[], ctx=ast.Load()))
            fields_node = success.values[-1]
        else:
            fields_node = success.values[fields_index]
        if not isinstance(fields_node, ast.List):
            return None
        existing = {
            _semantic_key(item.value)
            for item in fields_node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        }
        for field_name in fields:
            key = _semantic_key(field_name)
            if key in existing:
                continue
            fields_node.elts.append(ast.Constant(value=field_name))
            existing.add(key)
            changed = True

    if not changed:
        return None
    ast.fix_missing_locations(tree)
    return ast.unparse(tree).strip()


def validate_code(source: str) -> list[CodeDiagnostic]:
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
    visitor.diagnostics.extend(_date_constructor_diagnostics(function))
    visitor.diagnostics.extend(_undefined_name_diagnostics(source, function))
    visitor.diagnostics.extend(_ctx_state_contract_diagnostics(function))
    visitor.diagnostics.extend(_ctx_effect_lineage_diagnostics(function))
    visitor.diagnostics.extend(validate_projection_contract(source))
    return visitor.diagnostics


def _lookup_rows(
    lookups: dict[str, list[dict[str, Any]]],
    value: str,
) -> list[dict[str, Any]] | None:
    exact = lookups.get(value.strip().casefold())
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
    fields_node = _call_argument(
        node,
        "fields",
        1,
    )
    if isinstance(fields_node, (ast.List, ast.Tuple)):
        fields = [
            element.value
            for element in fields_node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        return fields if len(fields) == len(fields_node.elts) else None
    if isinstance(fields_node, ast.Dict):
        fields = [
            key.value
            for key in fields_node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        return fields if len(fields) == len(fields_node.keys) else None
    return None


def _literal_strings(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        str(value.value)
        for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }


_UNRESOLVED_LITERAL = object()


def _static_literal(
    node: ast.AST | None,
    names: dict[str, Any] | None = None,
) -> Any:
    if node is None:
        return _UNRESOLVED_LITERAL
    if isinstance(node, ast.Name) and names is not None:
        return names.get(node.id, _UNRESOLVED_LITERAL)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_static_literal(item, names) for item in node.elts]
        if _UNRESOLVED_LITERAL in values:
            return _UNRESOLVED_LITERAL
        if isinstance(node, ast.Tuple):
            return tuple(values)
        if isinstance(node, ast.Set):
            return set(values)
        return values
    if isinstance(node, ast.Dict):
        keys = [_static_literal(item, names) for item in node.keys]
        values = [_static_literal(item, names) for item in node.values]
        if _UNRESOLVED_LITERAL in keys or _UNRESOLVED_LITERAL in values:
            return _UNRESOLVED_LITERAL
        return dict(zip(keys, values))
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        return _UNRESOLVED_LITERAL


def _literal_mapping(
    node: ast.AST | None,
    names: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    result = _static_literal(node, names)
    return (
        result
        if isinstance(result, dict)
        and all(isinstance(key, str) for key in result)
        else None
    )


def _referenced_fields(node: ast.AST) -> set[str]:
    fields: set[str] = set()
    for value in ast.walk(node):
        if (
            isinstance(value, ast.Subscript)
            and isinstance(value.slice, ast.Constant)
            and isinstance(value.slice.value, str)
        ):
            fields.add(value.slice.value)
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and value.args
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            fields.add(value.args[0].value)
    return fields


def build_probe_fixture(source: str) -> FixtureSpec:
    """Synthesize non-authoritative rows so production review can execute Python."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FixtureSpec()
    assignments = sorted(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    constant_values: dict[str, Any] = {}
    for assignment in assignments:
        value = _static_literal(assignment.value, constant_values)
        if value is not _UNRESOLVED_LITERAL:
            constant_values[assignment.targets[0].id] = value
    constant_mappings = {
        name: value
        for name, value in constant_values.items()
        if isinstance(value, dict)
    }
    inferred: dict[str, Any] = {}
    for comparison in (node for node in ast.walk(tree) if isinstance(node, ast.Compare)):
        for field_name in _referenced_fields(comparison.left):
            values = {
                value
                for comparator in comparison.comparators
                for value in _literal_strings(comparator)
            }
            if values:
                inferred.setdefault(_semantic_key(field_name), next(iter(values)))
        for comparator in comparison.comparators:
            for field_name in _referenced_fields(comparator):
                values = _literal_strings(comparison.left)
                if values:
                    inferred.setdefault(_semantic_key(field_name), next(iter(values)))

    row_count = max(
        [3, *[
            value.value
            for value in ast.walk(tree)
            if isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
            and 1 <= value.value <= 10
        ]],
    )

    def probe_value(field_name: str, index: int) -> Any:
        key = _semantic_key(field_name)
        if key in inferred:
            return inferred[key]
        if key in {_semantic_key(value) for value in IDENTITY_FIELDS}:
            return f"probe-{index + 1}"
        if "date" in key or "time" in key:
            return f"Jul {24 - index}, 2026 9:00:00 AM"
        if "status" in key:
            return "Complete"
        if any(token in key for token in ("amount", "cost", "payment", "price")) or (
            "total" in key
            and not any(token in key for token in ("count", "qty", "quantity", "record", "result"))
        ):
            return f"${1000 + index + 1:,.2f}"
        if any(token in key for token in (
            "count", "qty", "quantity", "rating", "result", "use",
        )):
            return index + 1
        return f"probe-{index + 1}"

    def probe_filter_value(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        lowered = {
            str(key).strip().casefold(): item
            for key, item in value.items()
        }
        return next(
            (lowered[key] for key in ("from", "start", "min", "to", "end", "max")
             if key in lowered),
            next(iter(lowered.values()), value),
        )

    lookups: dict[str, list[dict[str, Any]]] = {}
    for call in (node for node in ast.walk(tree) if _ctx_method(node) == "query"):
        fields = _literal_fields(call)
        names = set(_literal_strings(_call_argument(call, "entity", 0)))
        if fields is None or not names:
            continue
        filter_node = _call_argument(call, "filters", 2)
        filters = _literal_mapping(filter_node, constant_values)
        if filters is None and isinstance(filter_node, ast.Name):
            filters = constant_mappings.get(filter_node.id)
        if filters is None and isinstance(filter_node, ast.Dict):
            filters = {
                key.value: _UNRESOLVED_LITERAL
                for key in filter_node.keys
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            }
        filters = filters or {}
        rows = [
            {
                **{field: probe_value(field, index) for field in fields},
                **{
                    field: (
                        probe_value(field, index)
                        if value is _UNRESOLVED_LITERAL
                        else probe_filter_value(value)
                    )
                    for field, value in filters.items()
                },
            }
            for index in range(row_count)
        ]
        for name in names:
            key = name.strip().casefold()
            existing = lookups.get(key)
            if existing is None:
                lookups[key] = rows
                continue
            for current, row in zip(existing, rows):
                current.update({
                    field: value
                    for field, value in row.items()
                    if field not in current
                })

    read_calls = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _ctx_method(call) == "read"
    ]
    read_fields = {
        field
        for call in read_calls
        for field in (_literal_fields(call) or [])
    }
    reads = {
        _target_key(row): {
            field: probe_value(field, index % row_count) for field in read_fields
        }
        for index, row in enumerate(
            row for rows in lookups.values() for row in rows
        )
    } if read_fields else {}
    direct_read_fields = {
        field
        for call in read_calls
        if _call_argument(call, "target", 0) is None
        for field in (_literal_fields(call) or [])
    }
    if direct_read_fields:
        # Direct ctx.read(fields=...) reads the active UI itself.  The
        # fixture context represents an omitted target as None, whose canonical
        # key is "None"; seed that state even when the program has no query rows.
        reads[_target_key(None)] = {
            field: probe_value(field, 0) for field in direct_read_fields
        }
    commands = {
        value: True
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and _ctx_method(call) == "command"
        for value in _literal_strings(_call_argument(call, "capability", 0))
    }
    return FixtureSpec(
        lookups=lookups,
        reads=reads,
        command_results=commands,
    )


def validate_fixture_contract(
    source: str,
    fixture: FixtureSpec,
    *,
    match_lookup_sources: bool = False,
) -> list[CodeDiagnostic]:
    """Report literal fields that no supplied mock source can provide."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    collection_fields = {_semantic_key(field_name) for field_name in fixture.fields()}
    detail_fields = {
        _semantic_key(field_name)
        for detail in fixture.reads.values()
        for field_name in detail
    }
    reach_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _ctx_method(node) == "reach"
    ]
    diagnostics: list[CodeDiagnostic] = []
    for node in ast.walk(tree):
        method = _ctx_method(node)
        if method not in {"query", "read"}:
            continue
        requested = _literal_fields(node)
        if requested is None:
            continue
        available = collection_fields if method == "query" else detail_fields
        if match_lookup_sources and method == "query":
            aliases = {
                _semantic_key(value)
                for value in _literal_strings(_call_argument(node, "entity", 0))
            }
            matching_rows = [
                row
                for alias, rows in fixture.lookups.items()
                if _semantic_key(alias) in aliases
                for row in rows
            ]
            if not matching_rows:
                if not any(line < node.lineno for line in reach_lines):
                    diagnostics.append(_diag(
                        node,
                        "QUERY_CONTEXT_REQUIRED",
                        (
                            "query source is absent from the current observation; "
                            "establish its application context with ctx.reach first"
                        ),
                    ))
                continue
            available = {
                _semantic_key(field_name)
                for row in matching_rows
                for field_name in row
            }
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
                    f"ctx.{method} fields {missing!r} are unavailable from every "
                    f"supplied mock {method} source"
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
        if _ctx_method(node) != method:
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
            and not isinstance(node.slice, ast.Slice)
        ):
            return self.sequence_fields.get(node.value.id)
        return None

    def _infer_sequence(self, node: ast.AST) -> set[str] | None:
        query_fields = self._ctx_fields(node, "query")
        if query_fields is not None:
            return query_fields
        if isinstance(node, ast.Name):
            return self.sequence_fields.get(node.id)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Slice)
        ):
            return self._infer_sequence(node.value)
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
        self._bind(node.target, mapping=self._infer_sequence(node.iter))
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        saved_mappings = dict(self.mapping_fields)
        for generator in node.generators:
            self.visit(generator.iter)
            self._bind(generator.target, mapping=self._infer_sequence(generator.iter))
            for condition in generator.ifs:
                self.visit(condition)
        self.visit(node.elt)
        self.mapping_fields = saved_mappings

    visit_GeneratorExp = visit_ListComp

    def visit_Call(self, node: ast.Call) -> None:
        ranker = (
            node.func.id
            if isinstance(node.func, ast.Name)
            and node.func.id in {"sorted", "min", "max"}
            else ""
        )
        key = next((item.value for item in node.keywords if item.arg == "key"), None)
        if (
            ranker
            and node.args
            and isinstance(key, ast.Lambda)
            and len(key.args.args) == 1
        ):
            self.visit(node.args[0])
            fields = self._infer_sequence(node.args[0])
            parameter = key.args.args[0].arg
            previous = self.mapping_fields.get(parameter)
            if fields is not None:
                self.mapping_fields[parameter] = set(fields)
            self.visit(key.body)
            if previous is None:
                self.mapping_fields.pop(parameter, None)
            else:
                self.mapping_fields[parameter] = previous
            for argument in node.args[1:]:
                self.visit(argument)
            for keyword in node.keywords:
                if keyword.arg != "key":
                    self.visit(keyword.value)
            return
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
    """Check field access against literal fields projected by query/read."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    visitor = _ProjectionVisitor()
    visitor.visit(tree)
    return visitor.diagnostics


def validate_runtime_dataflow(source: str) -> list[CodeDiagnostic]:
    """Report assigned runtime capabilities or values that are never consumed."""
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
        if _ctx_method(value) == "read":
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
        message = (
            f"runtime-derived value {variable!r} is assigned but never used; consume the "
            "observed value in a dependent call, calculation, or return, or delete the "
            "unnecessary assignment and its ctx call"
        )
        diagnostics.append(_diag(
            node,
            "UNUSED_RUNTIME_VALUE",
            message,
        ))
    return diagnostics


def _field_value(mapping: dict[str, Any], field_name: str) -> tuple[bool, Any]:
    key = (
        field_name
        if field_name in mapping
        else next(
            (key for key in mapping if _semantic_key(str(key)) == _semantic_key(field_name)),
            None,
        )
    )
    return key is not None, mapping.get(key)


def _literal_filter_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        bounds = {
            str(key).strip().casefold(): canonical_filter_value(value)
            for key, value in expected.items()
        }
        actual_value = canonical_filter_value(actual)
        lower = next(
            (bounds[key] for key in ("from", "start", "min") if key in bounds),
            None,
        )
        upper = next(
            (bounds[key] for key in ("to", "end", "max") if key in bounds),
            None,
        )
        return (
            (lower is None or actual_value >= lower)
            and (upper is None or actual_value <= upper)
        )
    if isinstance(actual, str) and isinstance(expected, str):
        return expected.strip().casefold() in actual.strip().casefold()
    return actual == expected


def _identity_values(mapping: dict[str, Any]):
    return (
        str(mapping[field_name])
        for field_name in IDENTITY_FIELDS
        if mapping.get(field_name) is not None
    )


def _target_key(target: Any) -> str:
    if isinstance(target, dict):
        return next(
            _identity_values(target),
            repr(sorted(target.items(), key=lambda item: str(item[0]))),
        )
    return str(target)


class _FixtureContext:
    def __init__(self, fixture: FixtureSpec) -> None:
        self.fixture = fixture
        self.trace: list[TraceEvent] = []
        self.writes: list[WriteEvent] = []
        self._current_ui: CurrentUI | None = None
        self.current_target: Any = None
        self.read_aliases: dict[str, str] = {}
        self.state: dict[str, dict[str, Any]] = {}
        for rows in fixture.lookups.values():
            for row in rows:
                canonical = str(row.get("id") or row.get("ID") or "")
                if not canonical:
                    continue
                self.state.setdefault(canonical, {}).update(copy.deepcopy(row))
                self.read_aliases.update(
                    {value: canonical for value in _identity_values(row)}
                )
        for target_id, detail in fixture.reads.items():
            canonical = self.read_aliases.get(str(target_id), str(target_id))
            self.state.setdefault(canonical, {}).update(copy.deepcopy(detail))
            self.read_aliases[str(target_id)] = canonical
        for canonical, state in self.state.items():
            self.read_aliases.update(
                {value: canonical for value in _identity_values(state)}
            )

    def query(
        self,
        *,
        entity: str,
        fields: list[str] | dict[str, str],
        filters: dict[str, Any] | None = None,
        coverage: str = "complete",
    ) -> list[dict[str, Any]]:
        field_names, field_types = field_projection(fields)
        state = require_current_ui(self._current_ui, entity=entity)
        requested_filters = dict(json_value(dict(filters or {})))
        rows = _lookup_rows(self.fixture.lookups, entity) or []
        missing_filters = [
            name for name in requested_filters
            if rows and not any(_field_value(row, name)[0] for row in rows)
        ]
        if missing_filters:
            raise KeyError(
                f"collection filter fields {missing_filters!r} are unavailable "
                f"for query {entity!r}"
            )
        rows = [
            row for row in rows
            if all(
                _field_value(row, field_name)[0]
                and _literal_filter_matches(
                    _field_value(row, field_name)[1],
                    expected,
                )
                for field_name, expected in requested_filters.items()
            )
        ]
        missing = [
            name for name in field_names
            if rows and not any(_field_value(row, name)[0] for row in rows)
        ]
        if missing:
            available_fields = sorted({
                str(field_name)
                for row in rows
                for field_name in row
            })
            raise KeyError(
                f"collection fields {missing!r} are unavailable for query {entity!r}; "
                f"available_fields={available_fields!r}"
            )
        projected = normalize_table_rows(
            [
                {name: _field_value(row, name)[1] for name in field_names}
                for row in rows
            ],
            field_types,
        )
        self.trace.append(TraceEvent(
            "query",
            (entity,),
            {
                "fields": field_names,
                "field_types": field_types,
                "ui_state": state.token,
                "filters": copy.deepcopy(requested_filters),
                "coverage": coverage,
            },
            projected,
        ))
        return projected

    def read(
        self,
        *,
        target: Any = None,
        fields: list[str] | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        state = require_current_ui(self._current_ui)
        if fields is None:
            raise TypeError("ctx.read requires fields")
        field_names, field_types = field_projection(fields)
        actual_target = (
            self.current_target
            if target is None
            else json_value(target)
        )
        key = _target_key(actual_target)
        key = self.read_aliases.get(key, key)
        if key not in self.state:
            raise KeyError(f"no fixture read state for target {key!r}")
        record = self.state[key]
        missing = [
            name for name in field_names
            if not _field_value(record, name)[0]
        ]
        if missing:
            raise KeyError(f"fixture target {key!r} has no fields {missing}")
        result = normalize_table_rows([{
            name: _field_value(record, name)[1]
            for name in field_names
        }], field_types)[0]
        self.trace.append(TraceEvent(
            "read",
            (actual_target,),
            {
                "fields": field_names,
                "field_types": field_types,
                "ui_state": state.token,
            },
            result,
        ))
        return result

    def _state_key_for_input(self, value: Any) -> str | None:
        if not isinstance(value, dict) or not value:
            return None
        direct_key = self.read_aliases.get(_target_key(value))
        if direct_key is not None:
            return direct_key
        matches = [
            key for key, state in self.state.items()
            if all(
                _field_value(state, str(field_name))[0]
                and _field_value(state, str(field_name))[1] == field_value
                for field_name, field_value in value.items()
            )
        ]
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
        return resolved[0] if len(resolved) == 1 else (None, None)

    def reach(
        self,
        goal: str,
        *,
        success: dict[str, Any],
        target: Any = None,
    ) -> None:
        normalized = reach_postcondition(json_value(success))
        if normalized is None:
            raise ValueError("ctx.reach success is not a structured state")
        normalized_target = json_value(target) if target is not None else None
        state = CurrentUI(
            token=f"fixture-ui:{len(self.trace) + 1}",
            postcondition=normalized,
            target=normalized_target,
        )
        self._world_task(
            goal,
            target=normalized_target,
            values={},
            trace_extra={"success": success},
        )
        self._current_ui = state

    def commit(
        self,
        goal: str,
        *,
        target: Any = None,
        values: dict[str, Any],
    ) -> None:
        normalized_target = json_value(target) if target is not None else None
        if normalized_target is not None:
            require_current_ui(self._current_ui, target=normalized_target)
        self._world_task(goal, target=normalized_target, values=values)
        self._current_ui = None

    def _world_task(
        self,
        task: str,
        *,
        target: Any,
        values: dict[str, Any],
        trace_extra: dict[str, Any] | None = None,
        result: Any = None,
    ) -> None:
        normalized_target = json_value(target) if target is not None else None
        inputs = (
            {"target": copy.deepcopy(normalized_target)}
            if normalized_target is not None
            else {}
        )
        desired_values = dict(json_value(copy.deepcopy(values)))
        state, key = self._statement_target(inputs)
        if state is not None:
            self.current_target = state
        if desired_values:
            state = self.state.get(key) if key is not None else None
            before = copy.deepcopy(state) if state is not None else {}
            for field_name, value in desired_values.items():
                if state is None:
                    break
                existing = next(
                    (
                        key_name for key_name in state
                        if _semantic_key(str(key_name)) == _semantic_key(str(field_name))
                    ),
                    field_name,
                )
                state[existing] = copy.deepcopy(value)
            self.writes.append(WriteEvent(
                goal=task,
                success=f"The GUI task is complete: {task}",
                target_id=key,
                inputs=copy.deepcopy(inputs),
                required_values=desired_values,
                observe_fields=[],
                persistence="explicit_commit",
                before=before,
                after=copy.deepcopy(state if state is not None else desired_values),
                applied=True,
            ))
        self.trace.append(TraceEvent(
            "commit" if desired_values else "reach",
            (task,),
            {
                "target": copy.deepcopy(inputs.get("target")),
                **copy.deepcopy(trace_extra or {}),
                "values": desired_values,
            },
            result,
        ))

    def command(self, capability: str, **kwargs: Any) -> Any:
        result = self.fixture.command_results.get(capability)
        self.trace.append(
            TraceEvent("command", (capability,), dict(json_value(kwargs)), result)
        )
        self._current_ui = None
        return result

def _worker(source: str, fixture: FixtureSpec, output: Any) -> None:
    ctx = _FixtureContext(fixture)
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "__name__": "coding_plan",
    }
    payload = {"trace": ctx.trace, "writes": ctx.writes, "final_state": ctx.state}
    try:
        exec(compile(source, "<coding-plan>", "exec"), namespace, namespace)
        payload.update(ok=True, return_value=namespace["run"](ctx))
    except BaseException:  # noqa: BLE001 - child must serialize any plan failure
        payload.update(ok=False, error=traceback.format_exc(limit=8))
    output.put(payload)


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
