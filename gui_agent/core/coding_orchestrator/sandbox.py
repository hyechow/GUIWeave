"""Restricted source validation and fixture execution for coding plans.

This is an experiment boundary, not a production-grade hostile-code sandbox.
The child process, AST allow-list and timeout keep evaluation failures isolated.
"""

from __future__ import annotations

import ast
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

from .models import CodeDiagnostic, CodingRunResult, TraceEvent


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
    "get", "index", "items", "join", "keys", "lower", "lstrip", "partition",
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
        ast.Nonlocal, ast.Raise, ast.While, ast.With, ast.Yield, ast.YieldFrom,
    )

    def __init__(self, root: ast.FunctionDef) -> None:
        self.root = root
        self.diagnostics: list[CodeDiagnostic] = []
        self.assertions: list[ast.Assert] = []
        self.local_functions: set[str] = set()
        self.safe_module_names: set[str] = set()

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
            self.safe_module_names.add(alias.asname or alias.name)

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
            if node.func.id not in SAFE_BUILTINS and node.func.id not in self.local_functions:
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
                {"goal"},
                {"goal", "success", "target", "values", "persistence"},
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
            "interact": ["goal", "success", "target", "values", "persistence"],
            "command": ["capability"],
            "compute": ["operation"],
        }[method]
        supplied.update(positional_names[:len(node.args)])
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
        persistence = keywords.get("persistence")
        if persistence is None and len(node.args) >= 5:
            persistence = node.args[4]
        if not isinstance(persistence, ast.Constant) or not isinstance(persistence.value, str):
            return
        if persistence.value not in {"immediate", "explicit_commit"}:
            self.diagnostics.append(_diag(
                persistence,
                "INTERACT_PERSISTENCE",
                "persistence must be 'immediate' or 'explicit_commit'",
            ))
            return


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
    return visitor.diagnostics


def _lookup_key(value: str) -> str:
    return value.strip().casefold()


def _semantic_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


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
            "id", "ID", "action_url", "Action_url", "Action", "sku", "SKU", "name", "Name",
            "title", "Title", "key",
        ):
            if field_name in target and target[field_name] is not None:
                return str(target[field_name])
    return str(target)


class _FixtureContext:
    def __init__(self, fixture: FixtureSpec) -> None:
        self.fixture = fixture
        self.trace: list[TraceEvent] = []
        self.current_target: Any = None
        self.read_aliases: dict[str, str] = {}
        for rows in fixture.lookups.values():
            for row in rows:
                canonical = str(row.get("id") or "")
                if not canonical or canonical not in fixture.reads:
                    continue
                for field_name in (
                    "id", "ID", "action_url", "Action_url", "Action", "sku", "SKU", "name", "Name",
                    "title", "Title", "key",
                ):
                    value = row.get(field_name)
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
        rows = self.fixture.lookups.get(_lookup_key(scope.entity))
        if rows is None and scope.fallback:
            rows = self.fixture.lookups.get(_lookup_key(scope.fallback))
        if rows is None:
            rows = []
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
        if key not in self.fixture.reads:
            raise KeyError(f"no fixture read state for target {key!r}")
        state = self.fixture.reads[key]
        missing = [name for name in fields if not _field_value(state, name)[0]]
        if missing:
            raise KeyError(f"fixture target {key!r} has no fields {missing}")
        result = {name: _field_value(state, name)[1] for name in fields}
        self.trace.append(TraceEvent("read", (actual_target,), {"fields": list(fields)}, result))
        return result

    def interact(
        self,
        goal: str,
        success: str | None = None,
        target: Any = None,
        values: dict[str, Any] | None = None,
        persistence: str = "immediate",
    ) -> bool:
        if target is not None:
            self.current_target = target
        if target is not None and values and persistence == "explicit_commit":
            key = _target_key(target)
            key = self.read_aliases.get(key, key)
            state = self.fixture.reads.get(key)
            if state is not None:
                for field_name, value in values.items():
                    existing = next(
                        (
                            key_name for key_name in state
                            if _semantic_key(str(key_name)) == _semantic_key(str(field_name))
                        ),
                        field_name,
                    )
                    state[existing] = value
        self.trace.append(TraceEvent("interact", (goal,), {
            "success": success or goal,
            "target": target,
            "values": dict(values or {}),
            "persistence": persistence,
        }))
        return True

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
        output.put({"ok": True, "return_value": result, "trace": ctx.trace})
    except BaseException:  # noqa: BLE001 - child must serialize any plan failure
        output.put({
            "ok": False,
            "error": traceback.format_exc(limit=8),
            "trace": ctx.trace,
        })


def execute_code(source: str, fixture: FixtureSpec, *, timeout: float = 2.0) -> CodingRunResult:
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
        error=str(payload.get("error") or ""),
    )


__all__ = ["CTX_METHODS", "FixtureSpec", "LookupScope", "execute_code", "validate_code"]
