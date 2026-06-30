"""A restricted expression evaluator for the DSL's `Compute` statement.

Compute is PURE derivation (string/number ops the interpreter does itself, not a GUI milestone) —
e.g. stripping a `-SIZE-COLOR` suffix off a variant name to get the parent base. To keep "it's just
code" without opening arbitrary execution, this evaluates a SMALL whitelist of a Python expression
over a flat scalar scope:

  - constants (str/int/float/bool/None), names (resolved from scope),
  - `+` (concat / numeric add), indexing and slicing `s[i]` / `s[a:b]`,
  - method calls on a string from a fixed whitelist (rsplit/split/strip/lower/replace/…),
  - a few module-free helper functions: re_sub, re_search, len, str, int, lower, upper.

No attribute access outside whitelisted str methods, no imports, no dunder, no comprehensions,
no arbitrary calls. Anything else raises SafeEvalError (the caller surfaces it as a run failure).
"""

from __future__ import annotations

import ast
import re
from typing import Any


class SafeEvalError(Exception):
    pass


_STR_METHODS = frozenset({
    "rsplit", "split", "strip", "lstrip", "rstrip", "lower", "upper", "replace",
    "removesuffix", "removeprefix", "title", "capitalize", "startswith", "endswith",
    "find", "rfind", "zfill", "join",
})


def _re_sub(pattern: str, repl: str, s: str) -> str:
    return re.sub(pattern, repl, s)


def _re_search(pattern: str, s: str, group: int = 0) -> str:
    m = re.search(pattern, s)
    return m.group(group) if m else ""


_FUNCS = {
    "re_sub": _re_sub, "re_search": _re_search,
    "len": len, "str": str, "int": int, "lower": str.lower, "upper": str.upper,
}


def safe_eval(expr: str, scope: dict[str, Any]) -> Any:
    """Evaluate a restricted expression against `scope`. Raises SafeEvalError on anything outside
    the whitelist (or a runtime error like bad index)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:  # noqa: BLE001
        raise SafeEvalError(f"语法错误: {e}") from e
    try:
        return _ev(tree.body, scope)
    except SafeEvalError:
        raise
    except Exception as e:  # noqa: BLE001 — bad index / type → honest failure, not a crash
        raise SafeEvalError(f"求值失败: {type(e).__name__}: {e}") from e


def _ev(node: ast.AST, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        if node.id in _FUNCS:
            return _FUNCS[node.id]
        raise SafeEvalError(f"未知变量: {node.id}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, right = _ev(node.left, scope), _ev(node.right, scope)
        return left % right if isinstance(node.op, ast.Mod) else left + right
    if isinstance(node, ast.Subscript):
        val = _ev(node.value, scope)
        sl = node.slice
        if isinstance(sl, ast.Slice):
            lo = _ev(sl.lower, scope) if sl.lower else None
            hi = _ev(sl.upper, scope) if sl.upper else None
            st = _ev(sl.step, scope) if sl.step else None
            return val[lo:hi:st]
        return val[_ev(sl, scope)]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_ev(e, scope) for e in node.elts]
    if isinstance(node, ast.Call):
        args = [_ev(a, scope) for a in node.args]
        kwargs = {kw.arg: _ev(kw.value, scope) for kw in node.keywords if kw.arg}
        f = node.func
        if isinstance(f, ast.Attribute):
            obj = _ev(f.value, scope)
            if isinstance(obj, str) and f.attr in _STR_METHODS:
                return getattr(obj, f.attr)(*args, **kwargs)
            raise SafeEvalError(f"不允许的方法调用: .{f.attr}()")
        if isinstance(f, ast.Name) and f.id in _FUNCS:
            return _FUNCS[f.id](*args, **kwargs)
        raise SafeEvalError("不允许的函数调用")
    raise SafeEvalError(f"不允许的表达式节点: {type(node).__name__}")
