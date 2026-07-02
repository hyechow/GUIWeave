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


def _coerce_num(x: Any) -> Any:
    """A page-read value is always a str ("75.00", "$45", "1,299"). For *, -, / that can only be
    numeric intent — parse the leading number (strip currency/grouping) so arithmetic works even when
    the decomposer forgot float(). Non-numeric strings pass through unchanged (the op then errors
    honestly)."""
    if not isinstance(x, str):
        return x
    m = re.search(r"-?\d[\d,]*\.?\d*", x)
    if not m:
        return x
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return x


def _re_sub(pattern: str, repl: str, s: str) -> str:
    return re.sub(pattern, repl, s)


def _re_search(pattern: str, s: str, group: int = 0) -> str:
    m = re.search(pattern, s)
    return m.group(group) if m else ""


def _lenient_float(x: object) -> float:
    """float() over a page read must tolerate currency/grouping ("$75.00", "1,299") — the value the
    author sees on screen IS the number; failing on the glyphs makes float(price) a trap."""
    coerced = _coerce_num(x)
    return float(coerced)  # non-numeric still raises honestly


def _lenient_int(x: object) -> int:
    coerced = _coerce_num(x)
    return int(float(coerced)) if isinstance(coerced, float) else int(coerced)


_FUNCS = {
    "re_sub": _re_sub, "re_search": _re_search,
    "len": len, "str": str, "int": _lenient_int, "lower": str.lower, "upper": str.upper,
    # Numeric derivation (WebArena 778 percentage price change): both decompose attempts naturally
    # wrote round(float(current_price) * 0.865, 2) — without these the whole numeric-compute class
    # silently degraded to "" and the fill milestone lost its concrete value.
    "float": _lenient_float, "round": round, "abs": abs,
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
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod, ast.Mult, ast.Sub, ast.Div)):
        left, right = _ev(node.left, scope), _ev(node.right, scope)
        # `+` (concat / add) and `%` (string format) keep string semantics. But *, -, / on a value
        # read off the page (always a str like "75.00") can only be numeric intent — coerce numeric
        # strings so `current_price * 0.865` works whether or not the decomposer wrapped float()
        # (WebArena 778: it wrote `round(current_price * 0.865, 2)` → "can't multiply sequence").
        if isinstance(node.op, (ast.Mult, ast.Sub, ast.Div)):
            left, right = _coerce_num(left), _coerce_num(right)
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Div):
            return left / right
        return left + right
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        val = _ev(node.operand, scope)
        return -val if isinstance(node.op, ast.USub) else +val
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _ev(node.operand, scope)
    # Comparisons / boolean logic / ternary: the decomposer (especially the Python-surface arm)
    # naturally writes membership predicates like `'-28-' in sku`, `a == b and c`, `x if cond else y`
    # (live 124348 wrote `'size 28' in row['name'].lower() or ...` → "不允许的表达式节点: Compare"
    # → silently ""). All pure, deterministic, no new capability surface.
    if isinstance(node, ast.Compare):
        left = _ev(node.left, scope)
        for op, comparator in zip(node.ops, node.comparators):
            right = _ev(comparator, scope)
            if isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                l_c, r_c = _coerce_num(left), _coerce_num(right)
                ok = (l_c < r_c if isinstance(op, ast.Lt) else l_c <= r_c if isinstance(op, ast.LtE)
                      else l_c > r_c if isinstance(op, ast.Gt) else l_c >= r_c)
            elif isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            else:
                raise SafeEvalError(f"不允许的比较运算: {type(op).__name__}")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            val = True
            for v in node.values:
                val = _ev(v, scope)
                if not val:
                    return val
            return val
        val = False
        for v in node.values:
            val = _ev(v, scope)
            if val:
                return val
        return val
    if isinstance(node, ast.IfExp):
        return _ev(node.body, scope) if _ev(node.test, scope) else _ev(node.orelse, scope)
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
