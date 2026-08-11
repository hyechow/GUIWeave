"""Reviewed-Python Master for orchestrating autonomous tool-agent Workers.

The program produced here describes task-level control and data flow.  It is
deliberately unable to issue GUI actions: those remain inside ``gui_worker`` and
its screenshot-driven loop.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from jsonschema import Draft202012Validator
from langchain_core.messages import HumanMessage, SystemMessage

from gui_agent.core.tool_agent.contracts import (
    DataRequirement,
    ResultRef,
    WorkerOutcome,
    WorkerSpec,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import (
    diagnostic_prompt_reports,
    message_text,
    response_usage,
    validate_dynamic_action_spec,
)
from gui_agent.core.tool_agent.sandbox import (
    execute_transform,
    validate_transform_row_fields,
    validate_transform_source,
)


_MASTER_FILENAME = "<tool-agent-master>"
_WORKER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CTX_METHODS = {
    "gui_worker",
    "transform",
    "worker_result",
    "finish",
    "replan",
    "fail",
}
_SAFE_METHODS = {
    "append",
    "copy",
    "count",
    "endswith",
    "extend",
    "get",
    "index",
    "items",
    "join",
    "keys",
    "lower",
    "pop",
    "setdefault",
    "sort",
    "split",
    "startswith",
    "strip",
    "upper",
    "values",
}
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
_BANNED_NAMES = {
    "__builtins__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "open",
    "setattr",
    "vars",
}
_BANNED_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Lambda,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.While,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


@dataclass(frozen=True)
class MasterDiagnostic:
    code: str
    message: str
    line: int = 0

    def render(self) -> str:
        location = f"line {self.line}" if self.line else "source"
        return f"[{self.code}] {location}: {self.message}"


@dataclass(frozen=True)
class MasterProgram:
    source: str
    attempts: int


@dataclass(frozen=True)
class MasterTerminal:
    phase: Literal["completed", "failed", "replan"]
    summary: str
    result_ref: str = ""
    effect: Literal["mutation", "data", "ui_state", "none"] = "none"


@dataclass(frozen=True)
class MasterExecution:
    terminal: MasterTerminal | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.terminal is not None and not self.error


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    signature: str
    outcome: WorkerOutcome


class MasterCompileError(ValueError):
    pass


class _ProgramHalt(BaseException):
    pass


def _diagnostic(code: str, message: str, node: ast.AST | None = None) -> MasterDiagnostic:
    return MasterDiagnostic(code=code, message=message, line=getattr(node, "lineno", 0))


def _literal_keyword(call: ast.Call, name: str) -> Any:
    keyword = next((item for item in call.keywords if item.arg == name), None)
    if keyword is None:
        raise ValueError(f"missing required keyword {name!r}")
    try:
        return ast.literal_eval(keyword.value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise ValueError(f"keyword {name!r} must be a literal value") from exc


def _validate_worker_id(call: ast.Call) -> list[MasterDiagnostic]:
    try:
        worker_id = _literal_keyword(call, "worker_id")
    except ValueError as exc:
        return [_diagnostic("WORKER_ID", str(exc), call)]
    if not isinstance(worker_id, str) or _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
        return [_diagnostic("WORKER_ID", "worker_id must be a stable snake_case identifier", call)]
    return []


def _validate_gui_worker_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics = _validate_worker_id(call)
    required = {
        "goal",
        "success_criteria",
        "actions",
    }
    values: dict[str, Any] = {}
    for name in required:
        try:
            values[name] = _literal_keyword(call, name)
        except ValueError as exc:
            diagnostics.append(_diagnostic("GUI_WORKER_LITERAL", str(exc), call))
    if diagnostics:
        return diagnostics
    if any(item.arg == "data_requirements" for item in call.keywords):
        try:
            values["data_requirements"] = _literal_keyword(call, "data_requirements")
        except ValueError as exc:
            diagnostics.append(_diagnostic("GUI_WORKER_LITERAL", str(exc), call))
    else:
        try:
            explicit_profile = _literal_keyword(call, "profile")
        except ValueError:
            explicit_profile = None
        if explicit_profile == "operator":
            values["data_requirements"] = []
        else:
            diagnostics.append(_diagnostic(
                "GUI_WORKER_LITERAL",
                "missing required keyword 'data_requirements' unless profile='operator'",
                call,
            ))
    if diagnostics:
        return diagnostics
    max_steps = 12
    if any(item.arg == "max_steps" for item in call.keywords):
        try:
            max_steps = _literal_keyword(call, "max_steps")
        except ValueError as exc:
            return [_diagnostic("GUI_WORKER_LITERAL", str(exc), call)]
    try:
        profile = None
        if any(item.arg == "profile" for item in call.keywords):
            profile = _literal_keyword(call, "profile")
        acquisition_filters = None
        if any(item.arg == "acquisition_filters" for item in call.keywords):
            acquisition_filters = _literal_keyword(call, "acquisition_filters")
        spec = WorkerSpec.model_validate(
            {
                **values,
                "profile": profile,
                "input_refs": {},
                "acquisition_filters": acquisition_filters,
                "max_steps": max_steps,
            }
        )
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        for action in spec.actions:
            validate_dynamic_action_spec(action)
    except Exception as exc:  # noqa: BLE001 - surfaced as a compile diagnostic
        diagnostics.append(_diagnostic("GUI_WORKER_SPEC", str(exc), call))
    input_keyword = next((item for item in call.keywords if item.arg == "input_refs"), None)
    input_names: set[str] = set()
    if input_keyword is not None:
        if isinstance(input_keyword.value, ast.Constant) and input_keyword.value.value is None:
            pass
        elif not isinstance(input_keyword.value, ast.Dict):
            diagnostics.append(_diagnostic(
                "GUI_WORKER_INPUT_REFS",
                "input_refs must be an inline dict of name: result_descriptor['ref']",
                input_keyword.value,
            ))
        else:
            for key, value in zip(
                input_keyword.value.keys,
                input_keyword.value.values,
                strict=True,
            ):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    diagnostics.append(_diagnostic(
                        "GUI_WORKER_INPUT_REFS",
                        "input_refs names must be literal strings",
                        key,
                    ))
                    continue
                input_names.add(key.value)
                if _subscript_key(value) != "ref":
                    diagnostics.append(_diagnostic(
                        "REF_VALUE_REQUIRED",
                        "gui_worker input_refs require ResultRef descriptor['ref'] values",
                        value,
                    ))
    action_input_names = {
        str(binding.get("input") or "")
        for action in values.get("actions") or []
        if isinstance(action, dict)
        for binding in (action.get("input_args") or {}).values()
        if isinstance(binding, dict)
    }
    unknown_inputs = action_input_names.difference(input_names)
    if unknown_inputs:
        diagnostics.append(_diagnostic(
            "GUI_WORKER_INPUT_BINDING",
            f"action input_args reference undeclared input_refs: {sorted(unknown_inputs)}",
            call,
        ))
    unused_inputs = input_names.difference(action_input_names)
    if unused_inputs:
        diagnostics.append(_diagnostic(
            "GUI_WORKER_INPUT_BINDING",
            f"input_refs must be consumed by deterministic action input_args: {sorted(unused_inputs)}",
            call,
        ))
    return diagnostics


def _validate_transform_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics: list[MasterDiagnostic] = []
    for name in ("transform_id", "source", "result_schema"):
        try:
            value = _literal_keyword(call, name)
            if name == "transform_id" and (
                not isinstance(value, str) or _WORKER_ID_PATTERN.fullmatch(value) is None
            ):
                raise ValueError("transform_id must be a stable snake_case identifier")
            if name == "source":
                if not isinstance(value, str):
                    raise ValueError("source must be a string")
                validate_transform_source(value)
            if name == "result_schema":
                Draft202012Validator.check_schema(value)
        except Exception as exc:  # noqa: BLE001 - one deterministic diagnostic channel
            diagnostics.append(_diagnostic("TRANSFORM_SPEC", f"{name}: {exc}", call))
    if not any(item.arg == "inputs" for item in call.keywords):
        diagnostics.append(_diagnostic("TRANSFORM_SPEC", "missing required keyword 'inputs'", call))
    else:
        inputs = next(item.value for item in call.keywords if item.arg == "inputs")
        if isinstance(inputs, (ast.List, ast.Tuple)):
            for item in inputs.elts:
                if _subscript_key(item) in {"collection_ref", "result_ref"}:
                    diagnostics.append(_diagnostic(
                        "REF_VALUE_REQUIRED",
                        "ctx.transform inputs require descriptor['ref'], not a descriptor object",
                        item,
                    ))
    return diagnostics


def _subscript_key(node: ast.AST) -> str:
    if not isinstance(node, ast.Subscript):
        return ""
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return ""


def _base_name(node: ast.AST) -> str:
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _subscript_path(node: ast.AST) -> tuple[str, tuple[str, ...]]:
    """Return the root name and literal string-key path for a reviewed expression."""

    keys: list[str] = []
    while isinstance(node, ast.Subscript):
        key = _subscript_key(node)
        if not key:
            return "", ()
        keys.append(key)
        node = node.value
    if not isinstance(node, ast.Name):
        return "", ()
    return node.id, tuple(reversed(keys))


def _ctx_call(node: ast.AST, method: str) -> ast.Call | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if not (
        isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
        and node.func.attr == method
    ):
        return None
    return node


def _static_transform_input_diagnostics(tree: ast.AST) -> list[MasterDiagnostic]:
    """Review transform row fields against statically routed Worker schemas."""

    row_schemas: dict[str, dict[str, Any]] = {}
    # The value represented by each row schema is reached through this exact path.
    # A local alias such as ``rows_ref = outcome["collection_ref"]["ref"]`` has
    # already resolved the descriptor path, so its expected path is empty.
    ref_paths: dict[str, tuple[str, ...]] = {}
    transform_calls: list[ast.Call] = []
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Assign)),
        key=lambda node: getattr(node, "lineno", 0),
    )
    for assignment in assignments:
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
            continue
        name = assignment.targets[0].id
        worker_call = _ctx_call(assignment.value, "gui_worker")
        if worker_call is not None:
            try:
                requirements = _literal_keyword(worker_call, "data_requirements")
                profile = (
                    _literal_keyword(worker_call, "profile")
                    if any(item.arg == "profile" for item in worker_call.keywords)
                    else None
                )
                if profile == "operator" or not requirements:
                    continue
                requirement = requirements[0]
                if isinstance(requirement, dict) and isinstance(
                    requirement.get("row_schema"), dict
                ):
                    row_schemas[name] = DataRequirement.model_validate(
                        requirement
                    ).row_schema
                    ref_paths[name] = ("collection_ref", "ref")
            except Exception:
                # The ordinary WorkerSpec review reports malformed requirements.
                continue
            continue
        transform_call = _ctx_call(assignment.value, "transform")
        if transform_call is None:
            base, path = _subscript_path(assignment.value)
            if base in row_schemas and path == ref_paths.get(base):
                row_schemas[name] = row_schemas[base]
                ref_paths[name] = ()
            elif (
                isinstance(assignment.value, ast.Name)
                and assignment.value.id in row_schemas
                and ref_paths.get(assignment.value.id) == ()
            ):
                row_schemas[name] = row_schemas[assignment.value.id]
                ref_paths[name] = ()
            continue
        transform_calls.append(transform_call)
        try:
            result_schema = _literal_keyword(transform_call, "result_schema")
        except ValueError:
            continue
        if not isinstance(result_schema, dict):
            continue
        items = result_schema.get("items")
        if result_schema.get("type") == "array" and isinstance(items, dict):
            row_schemas[name] = items
            ref_paths[name] = ("ref",)

    diagnostics: list[MasterDiagnostic] = []
    for call in transform_calls:
        inputs_node = next(
            (item.value for item in call.keywords if item.arg == "inputs"),
            None,
        )
        if not isinstance(inputs_node, (ast.List, ast.Tuple)):
            continue
        schemas = []
        for item in inputs_node.elts:
            name, path = _subscript_path(item)
            if name in row_schemas and path == ref_paths.get(name):
                schemas.append(row_schemas[name])
        if not schemas:
            continue
        combined = {
            "type": "object",
            "properties": {
                key: value
                for schema in schemas
                for key, value in (schema.get("properties") or {}).items()
            },
        }
        try:
            source = _literal_keyword(call, "source")
            validate_transform_row_fields(source, combined)
        except Exception as exc:  # noqa: BLE001 - one static diagnostic channel
            diagnostics.append(
                _diagnostic("TRANSFORM_INPUT_SCHEMA", str(exc), call)
            )
    return diagnostics


def _static_collector_consumption_diagnostics(tree: ast.AST) -> list[MasterDiagnostic]:
    """Require every collector to feed observed rows into deterministic data flow.

    A collector is not a navigation primitive.  If its CollectionRef is never consumed by a
    transform, splitting it from a later operator only forces visual navigation state through an
    unrepresentable data handle.  The cohesive operator should own that branch instead.
    """

    collectors: dict[str, ast.Call] = {}
    ref_aliases: dict[str, str] = {}
    consumed: set[str] = set()
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Assign)),
        key=lambda node: getattr(node, "lineno", 0),
    )
    for assignment in assignments:
        if (
            len(assignment.targets) != 1
            or not isinstance(assignment.targets[0], ast.Name)
        ):
            continue
        call = _ctx_call(assignment.value, "gui_worker")
        if call is None:
            continue
        try:
            requirements = _literal_keyword(call, "data_requirements")
            profile = (
                _literal_keyword(call, "profile")
                if any(item.arg == "profile" for item in call.keywords)
                else None
            )
        except ValueError:
            continue
        if profile == "collector" or (profile is None and requirements):
            collectors[assignment.targets[0].id] = call

    for assignment in assignments:
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
            continue
        alias = assignment.targets[0].id
        base, path = _subscript_path(assignment.value)
        if base in collectors and path == ("collection_ref", "ref"):
            ref_aliases[alias] = base
        elif (
            isinstance(assignment.value, ast.Name)
            and assignment.value.id in ref_aliases
        ):
            ref_aliases[alias] = ref_aliases[assignment.value.id]

    for node in ast.walk(tree):
        call = _ctx_call(node, "transform")
        if call is None:
            continue
        inputs = next((item.value for item in call.keywords if item.arg == "inputs"), None)
        if not isinstance(inputs, (ast.List, ast.Tuple)):
            continue
        for item in inputs.elts:
            base, path = _subscript_path(item)
            if base in collectors and path == ("collection_ref", "ref"):
                consumed.add(base)
            elif base in ref_aliases and not path:
                consumed.add(ref_aliases[base])

    return [
        _diagnostic(
            "COLLECTOR_RESULT_UNUSED",
            f"collector {name!r} must route its collection_ref into ctx.transform; "
            "use one cohesive operator when the Worker only locates/navigates to a record",
            call,
        )
        for name, call in collectors.items()
        if name not in consumed
    ]


def _static_result_routing_diagnostics(tree: ast.AST) -> list[MasterDiagnostic]:
    """Require computed ResultRefs to reach the next physical-effect Worker explicitly."""

    pending_results: set[str] = set()
    diagnostics: list[MasterDiagnostic] = []
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Assign)),
        key=lambda node: getattr(node, "lineno", 0),
    )
    for assignment in assignments:
        if len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name):
            continue
        assigned_name = assignment.targets[0].id
        transform_call = _ctx_call(assignment.value, "transform")
        if transform_call is not None:
            inputs = next(
                (item.value for item in transform_call.keywords if item.arg == "inputs"),
                None,
            )
            if isinstance(inputs, (ast.List, ast.Tuple)):
                pending_results.difference_update(
                    _base_name(value) for value in inputs.elts
                )
            pending_results.add(assigned_name)
            continue
        worker_call = _ctx_call(assignment.value, "gui_worker")
        if worker_call is None or not pending_results:
            continue
        keyword = next(
            (item for item in worker_call.keywords if item.arg == "input_refs"),
            None,
        )
        routed = {
            _base_name(value)
            for value in keyword.value.values
            if keyword is not None and isinstance(keyword.value, ast.Dict)
        } if keyword is not None and isinstance(keyword.value, ast.Dict) else set()
        missing = pending_results.difference(routed)
        if missing:
            diagnostics.append(_diagnostic(
                "RESULT_REF_UNROUTED",
                "ResultRefs computed before a GUI Worker must be routed through "
                f"that call's input_refs: {sorted(missing)}",
                worker_call,
            ))
        pending_results.difference_update(routed)
    return diagnostics


def _static_worker_array_input_diagnostics(tree: ast.AST) -> list[MasterDiagnostic]:
    """Reject private arrays routed directly into one visual Worker.

    ResultRefs deliberately hide their values from the model. The runtime can bind one scalar
    value into one selected action, but it has no implicit map/foreach operation that expands a
    private array into repeated GUI actions. The Master must compute one scalar/object target or
    delegate the cohesive mutation to the application's own bulk/group editor.
    """

    array_results: dict[str, ast.Call] = {}
    for assignment in ast.walk(tree):
        if (
            not isinstance(assignment, ast.Assign)
            or len(assignment.targets) != 1
            or not isinstance(assignment.targets[0], ast.Name)
        ):
            continue
        call = _ctx_call(assignment.value, "transform")
        if call is None:
            continue
        try:
            schema = _literal_keyword(call, "result_schema")
        except ValueError:
            continue
        if isinstance(schema, dict) and schema.get("type") == "array":
            array_results[assignment.targets[0].id] = call

    diagnostics: list[MasterDiagnostic] = []
    for node in ast.walk(tree):
        worker_call = _ctx_call(node, "gui_worker")
        if worker_call is None:
            continue
        keyword = next(
            (item for item in worker_call.keywords if item.arg == "input_refs"),
            None,
        )
        if keyword is None or not isinstance(keyword.value, ast.Dict):
            continue
        routed_arrays = sorted({
            name
            for value in keyword.value.values
            if (name := _base_name(value)) in array_results
        })
        if routed_arrays:
            diagnostics.append(_diagnostic(
                "WORKER_ARRAY_INPUT_UNSUPPORTED",
                "GUI Worker input_refs cannot expand private array ResultRefs into repeated "
                f"actions: {routed_arrays}; compute one scalar/object target, or use one "
                "cohesive Worker over the application's bulk/group editor",
                worker_call,
            ))
    return diagnostics


def _static_finish_ref_diagnostics(tree: ast.AST) -> list[MasterDiagnostic]:
    """Reject transform descriptor objects passed where finish requires their ref string."""

    transform_descriptors = {
        assignment.targets[0].id
        for assignment in ast.walk(tree)
        if isinstance(assignment, ast.Assign)
        and len(assignment.targets) == 1
        and isinstance(assignment.targets[0], ast.Name)
        and _ctx_call(assignment.value, "transform") is not None
    }
    diagnostics: list[MasterDiagnostic] = []
    for node in ast.walk(tree):
        call = _ctx_call(node, "finish")
        if call is None:
            continue
        value: ast.AST | None = call.args[0] if call.args else None
        keyword = next((item for item in call.keywords if item.arg == "result_ref"), None)
        if keyword is not None:
            value = keyword.value
        if isinstance(value, ast.Name) and value.id in transform_descriptors:
            diagnostics.append(_diagnostic(
                "REF_VALUE_REQUIRED",
                f"ctx.finish requires {value.id}['ref'], not the ResultRef descriptor",
                value,
            ))
    return diagnostics


def _validate_finish_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics: list[MasterDiagnostic] = []
    unknown_keywords = {
        item.arg for item in call.keywords if item.arg not in {"result_ref", "effect"}
    }
    if unknown_keywords:
        diagnostics.append(_diagnostic(
            "FINISH_SIGNATURE",
            f"ctx.finish received unknown keyword arguments: {sorted(unknown_keywords)}",
            call,
        ))
    if len(call.args) > 1:
        diagnostics.append(_diagnostic(
            "FINISH_SIGNATURE",
            "ctx.finish accepts one positional result ref; effect must be a keyword",
            call,
        ))
    value: ast.AST | None = call.args[0] if call.args else None
    keyword = next((item for item in call.keywords if item.arg == "result_ref"), None)
    if keyword is not None:
        value = keyword.value
    if value is None:
        diagnostics.append(_diagnostic(
            "FINISH_SIGNATURE", "ctx.finish requires a result ref string", call
        ))
    if _subscript_key(value) in {"collection_ref", "result_ref"}:
        diagnostics.append(_diagnostic(
            "REF_VALUE_REQUIRED",
            "ctx.finish requires result_ref['ref'], not the result_ref descriptor",
            value,
        ))
    effect_keyword = next((item for item in call.keywords if item.arg == "effect"), None)
    effect = (
        effect_keyword.value.value
        if effect_keyword is not None
        and isinstance(effect_keyword.value, ast.Constant)
        and isinstance(effect_keyword.value.value, str)
        else None
    )
    if effect not in {"mutation", "data", "ui_state"}:
        diagnostics.append(_diagnostic(
            "FINISH_EFFECT",
            "ctx.finish requires literal effect='mutation', 'data', or 'ui_state'",
            call,
        ))
    return diagnostics


def validate_master_source(source: str) -> list[MasterDiagnostic]:
    """Validate one restricted Worker-orchestration program."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return [MasterDiagnostic("SYNTAX", str(exc), exc.lineno or 0)]

    diagnostics: list[MasterDiagnostic] = []
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(tree.body) != 1 or len(functions) != 1 or functions[0].name != "run":
        diagnostics.append(_diagnostic("PROGRAM_SHAPE", "source must contain exactly def run(ctx):"))
    else:
        fn = functions[0]
        if (
            len(fn.args.args) != 1
            or fn.args.args[0].arg != "ctx"
            or fn.args.vararg is not None
            or fn.args.kwarg is not None
            or fn.args.kwonlyargs
            or fn.decorator_list
        ):
            diagnostics.append(_diagnostic("RUN_SIGNATURE", "run must accept exactly one argument named ctx", fn))

    terminal_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            diagnostics.append(_diagnostic("UNSAFE_SYNTAX", f"{type(node).__name__} is disallowed", node))
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            diagnostics.append(_diagnostic("UNSAFE_NAME", f"{node.id!r} is disallowed", node))
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                diagnostics.append(_diagnostic("PRIVATE_ATTRIBUTE", "private/dunder access is disallowed", node))
            elif not (
                isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
                or node.attr in _SAFE_METHODS
            ):
                diagnostics.append(_diagnostic(
                    "ATTRIBUTE_ACCESS",
                    "Master values are JSON-like dicts; use ['ref'] rather than object attributes",
                    node,
                ))
        if not isinstance(node, ast.Call):
            continue
        if any(item.arg is None for item in node.keywords):
            diagnostics.append(_diagnostic("KEYWORD_SPLAT", "**kwargs calls are disallowed", node))
        if isinstance(node.func, ast.Name):
            if node.func.id not in _SAFE_BUILTINS:
                diagnostics.append(_diagnostic("UNSAFE_CALL", f"call to {node.func.id!r} is disallowed", node))
            continue
        if not isinstance(node.func, ast.Attribute):
            diagnostics.append(_diagnostic("UNSAFE_CALL", "indirect calls are disallowed", node))
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx":
            method = node.func.attr
            if method not in _CTX_METHODS:
                diagnostics.append(_diagnostic("UNKNOWN_CTX_API", f"ctx.{method} is not available", node))
                continue
            if method in {"gui_worker", "transform"} and node.args:
                diagnostics.append(_diagnostic("CALL_SIGNATURE", f"ctx.{method} accepts keyword arguments only", node))
            if method == "gui_worker":
                diagnostics.extend(_validate_gui_worker_call(node))
            elif method == "transform":
                diagnostics.extend(_validate_transform_call(node))
            elif method in {"finish", "replan", "fail"}:
                terminal_calls += 1
                if method == "finish":
                    diagnostics.extend(_validate_finish_call(node))
            continue
        if node.func.attr not in _SAFE_METHODS:
            diagnostics.append(_diagnostic("UNSAFE_METHOD", f"method {node.func.attr!r} is disallowed", node))

    if terminal_calls == 0:
        diagnostics.append(_diagnostic("TERMINAL_REQUIRED", "program must call ctx.finish or ctx.fail"))
    diagnostics.extend(_static_transform_input_diagnostics(tree))
    diagnostics.extend(_static_collector_consumption_diagnostics(tree))
    diagnostics.extend(_static_result_routing_diagnostics(tree))
    diagnostics.extend(_static_worker_array_input_diagnostics(tree))
    diagnostics.extend(_static_finish_ref_diagnostics(tree))
    unique: list[MasterDiagnostic] = []
    seen: set[tuple[str, str, int]] = set()
    for item in diagnostics:
        key = (item.code, item.message, item.line)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _extract_source(content: Any) -> str:
    text = message_text(content).strip()
    fenced = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


def compile_master_program(
    *,
    llm: Any,
    system_prompt: str,
    task_context: dict[str, Any],
    max_attempts: int = 3,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> MasterProgram:
    """Generate and deterministically review a complete orchestration program."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    generator = (
        llm.bind(max_tokens=12_000, extra_body={"enable_thinking": False})
        if callable(getattr(llm, "bind", None))
        else llm
    )
    rejected = ""
    last_diagnostics: list[MasterDiagnostic] = []
    for attempt in range(1, max_attempts + 1):
        payload = {"task": task_context}
        if rejected:
            payload["rejected_program"] = rejected
            payload["validation_issues"] = [item.render() for item in last_diagnostics]
        started_at = time.perf_counter()
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        response = generator.invoke(messages)
        llm_elapsed_s = time.perf_counter() - started_at
        source = _extract_source(response.content)
        diagnostics = validate_master_source(source)
        if on_event is not None:
            on_event(
                "master_compile_attempt",
                {
                    "attempt": attempt,
                    "source": source,
                    "diagnostics": [item.render() for item in diagnostics],
                    "llm_elapsed_s": round(llm_elapsed_s, 3),
                    "token_usage": response_usage(response),
                    "context_reports": diagnostic_prompt_reports(
                        "tool_agent.master",
                        messages,
                        response,
                        parsed={
                            "source": source,
                            "diagnostics": [item.render() for item in diagnostics],
                        },
                        schema="Reviewed Python program",
                    ),
                },
            )
        if not diagnostics:
            return MasterProgram(source=source, attempts=attempt)
        rejected = source
        last_diagnostics = diagnostics
    rendered = "; ".join(item.render() for item in last_diagnostics)
    raise MasterCompileError(f"Master program failed review after {max_attempts} attempts: {rendered}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkerOrchestrationContext:
    """Runtime API exposed to reviewed Master programs."""

    def __init__(
        self,
        *,
        data_store: RuntimeDataStore,
        run_gui_worker: Callable[[str, WorkerSpec], WorkerOutcome],
        trace: Callable[..., None],
    ) -> None:
        self._data_store = data_store
        self._run_gui_worker = run_gui_worker
        self._trace = trace
        self._records: dict[str, WorkerRecord] = {}
        self._transforms: dict[str, tuple[str, ResultRef]] = {}
        self._terminal: MasterTerminal | None = None

    @property
    def terminal(self) -> MasterTerminal | None:
        return self._terminal

    def _reuse(self, worker_id: str, signature: str) -> dict[str, Any] | None:
        record = self._records.get(worker_id)
        if record is None:
            return None
        if record.signature != signature:
            raise ValueError(
                f"worker_id {worker_id!r} is already bound to a different specification; "
                "use a new worker_id for a changed subgoal"
            )
        if record.outcome.phase == "failed":
            self._trace(
                "master_worker_retry",
                worker_id=worker_id,
                kind="gui",
                prior_outcome=record.outcome.model_dump(mode="json"),
            )
            return None
        self._trace(
            "master_worker_reuse",
            worker_id=worker_id,
            kind="gui",
            outcome=record.outcome.model_dump(mode="json"),
        )
        return record.outcome.model_dump(mode="json")

    def gui_worker(
        self,
        *,
        worker_id: str,
        profile: Literal["operator", "collector"] | None = None,
        goal: str,
        success_criteria: list[str],
        input_refs: dict[str, str] | None = None,
        data_requirements: list[dict[str, Any]] | None = None,
        actions: list[dict[str, Any]],
        acquisition_filters: dict[str, Any] | None = None,
        max_steps: int = 12,
    ) -> dict[str, Any]:
        if _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
            raise ValueError("worker_id must be a stable snake_case identifier")
        routed_inputs = dict(input_refs or {})
        for ref in routed_inputs.values():
            self._data_store.result_descriptor(ref)
        spec = WorkerSpec.model_validate(
            {
                "profile": profile,
                "goal": goal,
                "success_criteria": success_criteria,
                "input_refs": routed_inputs,
                "data_requirements": data_requirements or [],
                "acquisition_filters": acquisition_filters,
                "actions": actions,
                "max_steps": max_steps,
            }
        )
        signature = hashlib.sha256(_canonical(spec.model_dump(mode="json")).encode()).hexdigest()
        reused = self._reuse(worker_id, signature)
        if reused is not None:
            return reused
        self._trace(
            "master_worker_dispatch",
            worker_id=worker_id,
            kind="gui",
            goal=goal,
            spec=spec.model_dump(mode="json"),
        )
        try:
            outcome = self._run_gui_worker(worker_id, spec)
        except Exception as exc:  # noqa: BLE001 - a Worker failure is typed program data
            outcome = WorkerOutcome(
                phase="failed",
                summary=f"GUI Worker runtime error: {type(exc).__name__}: {exc}",
                steps=0,
            )
        self._records[worker_id] = WorkerRecord(worker_id, signature, outcome)
        self._trace(
            "master_worker_result",
            worker_id=worker_id,
            kind="gui",
            outcome=outcome.model_dump(mode="json"),
        )
        return outcome.model_dump(mode="json")

    def transform(
        self,
        *,
        transform_id: str,
        inputs: list[str],
        source: str,
        result_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if _WORKER_ID_PATTERN.fullmatch(transform_id) is None:
            raise ValueError("transform_id must be a stable snake_case identifier")
        if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
            raise ValueError("transform inputs must be a list of data refs")
        Draft202012Validator.check_schema(result_schema)
        validate_transform_source(source)
        request = {"inputs": inputs, "source": source, "result_schema": result_schema}
        signature = hashlib.sha256(_canonical(request).encode()).hexdigest()
        prior = self._transforms.get(transform_id)
        if prior is not None:
            prior_signature, prior_descriptor = prior
            if prior_signature != signature:
                raise ValueError(
                    f"transform_id {transform_id!r} is already bound to a different specification"
                )
            self._trace(
                "transform_reused",
                transform_id=transform_id,
                result_ref=prior_descriptor.model_dump(mode="json"),
            )
            return prior_descriptor.model_dump(mode="json")
        row_schemas = [
            schema for ref in inputs
            if (schema := self._data_store.ref_row_schema(ref)) is not None
        ]
        if row_schemas:
            combined_row_schema = {
                "type": "object",
                "properties": {
                    key: value
                    for schema in row_schemas
                    for key, value in (schema.get("properties") or {}).items()
                },
            }
            validate_transform_row_fields(source, combined_row_schema)
        self._trace(
            "transform_started",
            transform_id=transform_id,
            inputs=inputs,
            source=source,
            result_schema=result_schema,
        )
        try:
            input_values = [self._data_store.resolve_value(item) for item in inputs]
            value = execute_transform(source, input_values, result_schema)
            descriptor = self._data_store.put_result(
                value,
                result_schema,
                summary=f"Deterministic transform {transform_id} completed.",
            )
        except Exception as exc:
            self._trace(
                "transform_failed",
                transform_id=transform_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._trace(
            "transform_completed",
            transform_id=transform_id,
            inputs=inputs,
            result_ref=descriptor.model_dump(mode="json"),
        )
        self._transforms[transform_id] = (signature, descriptor)
        return descriptor.model_dump(mode="json")

    def worker_result(self, worker_id: str) -> dict[str, Any] | None:
        record = self._records.get(worker_id)
        return record.outcome.model_dump(mode="json") if record is not None else None

    def finish(
        self,
        result_ref: str,
        *,
        effect: Literal["mutation", "data", "ui_state"],
    ) -> None:
        if not isinstance(result_ref, str):
            raise ValueError(
                "finish requires a ResultRef string such as result['ref'], "
                f"got {type(result_ref).__name__}"
            )
        descriptor = self._data_store.result_descriptor(result_ref)
        self._terminal = MasterTerminal(
            phase="completed",
            summary=descriptor.summary or "Coding Master accepted the ResultRef.",
            result_ref=descriptor.ref,
            effect=effect,
        )
        raise _ProgramHalt

    def replan(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("replan requires a concrete reason")
        # Backward-compatible seal for already-recorded programs. Local strategy
        # revision now happens inside gui_worker before its outcome returns, so a
        # frozen program must never replay itself to retry the same logical Worker.
        self._terminal = MasterTerminal(phase="failed", summary=reason.strip())
        raise _ProgramHalt

    def fail(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("fail requires a concrete reason")
        self._terminal = MasterTerminal(phase="failed", summary=reason.strip())
        raise _ProgramHalt

    def reset_terminal(self) -> None:
        self._terminal = None


def execute_master_program(
    source: str,
    ctx: WorkerOrchestrationContext,
    *,
    max_lines: int = 10_000,
) -> MasterExecution:
    """Execute one reviewed orchestration program with a bounded line budget."""
    diagnostics = validate_master_source(source)
    if diagnostics:
        return MasterExecution(error="; ".join(item.render() for item in diagnostics))
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    ctx.reset_terminal()
    try:
        exec(compile(source, _MASTER_FILENAME, "exec"), namespace, namespace)
        lines = 0

        def tracer(frame: Any, event: str, arg: Any) -> Any:
            del arg
            nonlocal lines
            if event == "line" and frame.f_code.co_filename == _MASTER_FILENAME:
                lines += 1
                if lines > max_lines:
                    raise RuntimeError(f"Master program exceeded {max_lines} executed lines")
            return tracer

        previous_trace = sys.gettrace()
        sys.settrace(tracer)
        try:
            namespace["run"](ctx)
        except _ProgramHalt:
            pass
        finally:
            sys.settrace(previous_trace)
        if ctx.terminal is None:
            return MasterExecution(
                error="Master program returned without a terminal ctx call"
            )
        return MasterExecution(terminal=ctx.terminal)
    except BaseException as exc:  # noqa: BLE001 - sandbox boundary reports errors as data
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return MasterExecution(error=f"{type(exc).__name__}: {exc}")


__all__ = [
    "MasterCompileError",
    "MasterDiagnostic",
    "MasterExecution",
    "MasterProgram",
    "MasterTerminal",
    "WorkerOrchestrationContext",
    "compile_master_program",
    "execute_master_program",
    "validate_master_source",
]
