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
    "isinstance": isinstance,
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


_FENCE_LANGUAGE_TAGS = {"python", "py", "python3"}


def _strip_code_fence(source: str) -> str:
    """Remove a Markdown code fence copied into a transform source.

    Program examples in the Master prompt sit inside ```python fences, and the
    Master occasionally copies the fence or its language tag into the ``source``
    string. Both forms are unambiguous artifacts — a valid transform starts
    with ``def transform`` — so strip them at this boundary before validation
    and execution instead of relying on the model to learn from rejections.
    """
    text = source.strip()
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    text = "\n".join(lines).strip()
    first_line, separator, rest = text.partition("\n")
    if separator and first_line.strip().lower() in _FENCE_LANGUAGE_TAGS:
        text = rest.strip()
    return text


def validate_transform_source(source: str) -> None:
    source = _strip_code_fence(source)
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise TransformValidationError(f"invalid transform syntax: {exc}") from exc
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(tree.body) != 1 or len(functions) != 1 or functions[0].name != "transform":
        raise TransformValidationError(
            "source must contain exactly one top-level def transform(rows): "
            f"and nothing else (found {len(tree.body)} top-level statement(s))"
        )
    fn = functions[0]
    if len(fn.args.args) != 1 or fn.args.vararg or fn.args.kwarg:
        raise TransformValidationError("transform must accept exactly one positional argument")
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            if isinstance(node, ast.Try):
                hint = (
                    "; parse dates/text with regex or string methods instead of "
                    "try/except"
                )
            elif isinstance(node, ast.While):
                hint = "; use for loops or comprehensions instead of while"
            else:
                hint = ""
            raise TransformValidationError(
                f"disallowed syntax: {type(node).__name__}{hint}"
            )
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            raise TransformValidationError(f"disallowed name: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise TransformValidationError("private/dunder attribute access is disallowed")


def validate_transform_row_fields(
    source: str,
    row_schema: dict[str, Any],
) -> None:
    """Require transforms to read normalized schema keys, not display labels."""
    source = _strip_code_fence(source)
    validate_transform_source(source)
    tree = ast.parse(source, mode="exec")
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
    inputs_name = function.args.args[0].arg
    row_collection_names: set[str] = {inputs_name}
    row_names: set[str] = set()

    def add_targets(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            row_names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                add_targets(item)

    def collection_expression(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in row_collection_names
        if isinstance(node, ast.Subscript):
            return (
                isinstance(node.value, ast.Name)
                and node.value.id == inputs_name
            )
        return False

    # Transforms conventionally bind one runtime input before iterating it, e.g.
    # ``reviews = inputs[0]``. Track those aliases so row-field review cannot be
    # bypassed by an intermediate variable.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or not collection_expression(node.value):
                continue
            if target.id not in row_collection_names:
                row_collection_names.add(target.id)
                changed = True

    for node in ast.walk(function):
        if isinstance(node, ast.For) and collection_expression(node.iter):
            add_targets(node.target)
        if isinstance(node, ast.comprehension) and collection_expression(node.iter):
            add_targets(node.target)

    def row_field(node: ast.AST) -> str:
        while (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and not node.args
            and node.func.attr in {"casefold", "lower", "strip", "upper"}
        ):
            node = node.func.value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in row_names
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in row_names
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            return node.slice.value
        return ""

    properties = set((row_schema.get("properties") or {}).keys())
    used_fields = {field for node in ast.walk(function) if (field := row_field(node))}
    unknown = used_fields - properties
    if unknown:
        raise TransformValidationError(
            "transform reads fields outside normalized row_schema: "
            f"{sorted(unknown)}; available fields are {sorted(properties)}"
        )

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
    timeout_s: float = 10.0,
    max_input_bytes: int = 2_000_000,
    max_output_bytes: int = 1_000_000,
) -> Any:
    source = _strip_code_fence(source)
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
