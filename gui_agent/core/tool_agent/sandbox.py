"""Small process-isolated sandbox for Master-authored pure data transforms."""

from __future__ import annotations

import ast
import json
import multiprocessing
from typing import Any

from jsonschema import validate

_BANNED_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Global,
    ast.Nonlocal,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Raise,
    ast.While,
)
_BANNED_NAMES = {
    "open", "exec", "eval", "compile", "input", "breakpoint", "help",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "__builtins__", "memoryview", "classmethod", "staticmethod", "property",
}
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
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


class TransformValidationError(ValueError):
    pass


def validate_transform_source(source: str) -> None:
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise TransformValidationError(f"invalid transform syntax: {exc}") from exc
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(tree.body) != 1 or len(functions) != 1 or functions[0].name != "transform":
        raise TransformValidationError("source must contain exactly def transform(rows):")
    fn = functions[0]
    if len(fn.args.args) != 1 or fn.args.vararg or fn.args.kwarg:
        raise TransformValidationError("transform must accept exactly one positional argument")
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            raise TransformValidationError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise TransformValidationError(f"disallowed name: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise TransformValidationError("private/dunder attribute access is disallowed")


def _sandbox_child(connection: Any, source: str, rows: Any) -> None:
    try:
        namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
        exec(compile(source, "<tool-agent-transform>", "exec"), namespace, namespace)
        value = namespace["transform"](rows)
        encoded = json.dumps(value, ensure_ascii=False)
        connection.send((True, encoded))
    except BaseException as exc:  # noqa: BLE001 - exception crosses process boundary as text
        connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def execute_transform(
    source: str,
    rows: list[dict[str, Any]],
    output_schema: dict[str, Any],
    *,
    timeout_s: float = 2.0,
    max_input_bytes: int = 2_000_000,
    max_output_bytes: int = 1_000_000,
) -> Any:
    validate_transform_source(source)
    encoded_input = json.dumps(rows, ensure_ascii=False)
    if len(encoded_input.encode()) > max_input_bytes:
        raise TransformValidationError("transform input exceeds size limit")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_sandbox_child, args=(child, source, rows), daemon=True)
    process.start()
    child.close()
    if not parent.poll(timeout_s):
        process.terminate()
        process.join(timeout=1)
        raise TimeoutError(f"transform exceeded {timeout_s:g}s")
    ok, payload = parent.recv()
    process.join(timeout=1)
    if not ok:
        raise RuntimeError(f"transform failed: {payload}")
    if len(payload.encode()) > max_output_bytes:
        raise TransformValidationError("transform output exceeds size limit")
    value = json.loads(payload)
    validate(instance=value, schema=output_schema)
    return value
