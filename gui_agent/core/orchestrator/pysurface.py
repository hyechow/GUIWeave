"""Python-surface front-end for the orchestrator: compile a restricted-Python plan to Program IR.

WHY (2026-07-02, after the 778 saga): decompose reliability was diagnosed as mostly a LANGUAGE
SURFACE problem — the LLM one-shots a private JSON DSL it has never seen, so template conventions,
expression dialect, loop-var binding and branch shape all wobble (pass@1 85% on shape alone).
LLMs write Python far more reliably than any private DSL. This module keeps the ENTIRE backend
(Program IR, validator, preflight, interpreter, benchmark) and swaps only the authoring syntax:
the model writes a small Python program against a milestone API; we ast-parse and compile it to
the same Program the JSON path produces. A/B-measured against the JSON arm on the ground-truth
benchmark (scripts/orchestrator_groundtruth.py --surface python).

The surface (what the model writes):

    navigate("进入 Products List 页面", sc="页面显示产品列表")
    filter("清除残留筛选，搜索 'Sahara'", sc="列表显示 Sahara 相关记录（非0条）")
    for row in collect("size 28 的 Sahara 变体行", returns=["sku", "price", "action_url"]):
        d = navigate(f"打开变体 {row['sku']} 的编辑页", sc="已进入编辑页",
                     returns=["current_price"], read_spec="current_price: Price 输入框数值")
        new_price = round(float(d["current_price"]) * 0.865, 2)
        action(f"将价格更新为 {new_price} 并保存", sc="显示保存成功提示")
    finish("已完成所有 size 28 变体的调价")

Compile rules (deterministic, no execution):
  - navigate/filter/action/read/data_query calls → Run (assignment target = var binding)
  - `for row in collect(desc, returns=[...], limit=N):` → ForEach (body compiled recursively);
    a body that is a single `subgoal(f"...")` call → ForEach.body_goal (per-row agentic sub-goal)
  - plain assignment → Compute (expr must parse under safe_eval's whitelist — checked HERE, at
    compile time, so the runtime can never hit an unsupported expression: contract-derived validation)
  - `if v["field"] == "x": ... else: ...` → If/Cond (the supported comparison shapes only)
  - `finish(...)` → Finish; f-strings → the IR's {var[field]} / {scalar} templates
  - anything else (imports, while, try, def, comprehensions) → CompileIssue with a fix hint,
    fed back through the same retry loop as validator issues
"""

from __future__ import annotations

import ast
from typing import Optional

from .program import Compute, Cond, Finish, ForEach, If, Program, Query, Read, Run, Stmt
from .safe_eval import ProbeScope, SafeEvalError, safe_eval

_RUN_FUNCS = {"navigate": "navigation", "filter": "filter", "action": "action",
              "read": "read", "data_query": "data_query"}
_RUN_KWARGS = {"sc", "returns", "read_spec", "sql", "precondition", "data_scope"}


class CompileIssue(Exception):
    """One author-facing compile error (message is the retry feedback)."""


def _const_str(node: ast.AST, what: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _template_from_fstring(node)
    raise CompileIssue(f"{what} 必须是字符串字面量或 f-string（不能是表达式/变量）")


def _template_from_fstring(node: ast.JoinedStr) -> str:
    """f"打开 {row['sku']} 的页" → "打开 {row[sku]} 的页" — the IR's template convention."""
    parts: list[str] = []
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value))
        elif isinstance(v, ast.FormattedValue):
            parts.append(_template_ref(v.value))
        else:
            raise CompileIssue("f-string 里出现无法编译的片段")
    return "".join(parts)


def _template_ref(node: ast.AST) -> str:
    """One {…} inside an f-string: only var['field'] / var.field / bare scalar are addressable."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
            and isinstance(node.slice, ast.Constant):
        return "{" + f"{node.value.id}[{node.slice.value}]" + "}"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return "{" + f"{node.value.id}[{node.attr}]" + "}"
    if isinstance(node, ast.Name):
        return "{" + node.id + "}"
    raise CompileIssue(
        "f-string 里只能引用 变量['字段']、变量.字段 或 标量名；"
        f"不支持嵌入表达式「{ast.unparse(node)}」——先用一行赋值算出标量再引用"
    )


def _compile_run(call: ast.Call, var: Optional[str]) -> Run:
    kind = _RUN_FUNCS[call.func.id]  # type: ignore[union-attr]
    if not call.args:
        raise CompileIssue(f"{call.func.id}() 缺少第一个参数（里程碑描述）")  # type: ignore[union-attr]
    name = _const_str(call.args[0], "里程碑描述")
    kw: dict = {}
    for k in call.keywords:
        if k.arg not in _RUN_KWARGS:
            raise CompileIssue(f"{call.func.id}() 不支持关键字参数 {k.arg}（可用：{sorted(_RUN_KWARGS)}）")  # type: ignore[union-attr]
        if k.arg == "returns":
            if not isinstance(k.value, (ast.List, ast.Tuple)) or not all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in k.value.elts):
                raise CompileIssue("returns 必须是字符串列表字面量")
            kw["returns"] = [e.value for e in k.value.elts]  # type: ignore[union-attr]
        elif k.arg == "precondition":
            if not isinstance(k.value, ast.Constant) or not isinstance(k.value.value, bool):
                raise CompileIssue("precondition 必须是 True/False 字面量")
            kw["precondition"] = k.value.value
        else:
            kw["sc" if k.arg == "sc" else k.arg] = _const_str(k.value, f"{k.arg} 参数")
    common = dict(
        var=var, name=name,
        success_condition=kw.pop("sc", ""),
        returns=kw.pop("returns", []),
        read_spec=kw.pop("read_spec", ""),
    )
    if kind == "read":
        return Read(**common)
    if kind == "data_query":
        return Query(**common, sql=kw.pop("sql", ""), data_scope=kw.pop("data_scope", "complete"))
    return Run(**common, kind=kind, precondition=kw.pop("precondition", False))


def _compute_expr(node: ast.AST) -> str:
    """A pure-compute RHS: unparse and dry-parse under safe_eval so the runtime can never hit an
    unsupported expression (compile-time enforcement of the runtime contract)."""
    if isinstance(node, ast.JoinedStr):
        # f-string as a computed value: compile to concatenation of str() pieces.
        pieces = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                pieces.append(repr(str(v.value)))
            elif isinstance(v, ast.FormattedValue):
                pieces.append(f"str({ast.unparse(v.value)})")
        expr = " + ".join(pieces) if pieces else "''"
    else:
        expr = ast.unparse(node)
    try:
        safe_eval(expr, ProbeScope())
    except SafeEvalError as e:
        msg = str(e)
        if "未知变量" not in msg:  # unknown names are fine at compile time; dialect errors are not
            raise CompileIssue(f"表达式「{expr}」运行时不支持：{msg}——只用 round/float/str/len/abs/re_sub/re_search、"
                               "算术、比较、and/or、三元、切片和字符串方法")
    return expr


_CMP_MAP = {ast.Eq: "==", ast.NotEq: "!="}


def _compile_cond(test: ast.AST) -> Cond:
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, right = test.ops[0], test.comparators[0]
        left = test.left
        # v["field"] ==/!= "literal"
        if isinstance(left, ast.Subscript) and isinstance(left.value, ast.Name) \
                and isinstance(left.slice, ast.Constant) and type(op) in _CMP_MAP \
                and isinstance(right, ast.Constant):
            return Cond(var=left.value.id, field=str(left.slice.value),
                        cmp=_CMP_MAP[type(op)], value=str(right.value))
        # "literal" in v["field"]  → contains
        if isinstance(op, (ast.In, ast.NotIn)) and isinstance(left, ast.Constant) \
                and isinstance(right, ast.Subscript) and isinstance(right.value, ast.Name) \
                and isinstance(right.slice, ast.Constant):
            return Cond(var=right.value.id, field=str(right.slice.value),
                        cmp="contains" if isinstance(op, ast.In) else "not_contains",
                        value=str(left.value))
        # v["field"] in ("a", "b")  → in
        if isinstance(op, (ast.In, ast.NotIn)) and isinstance(left, ast.Subscript) \
                and isinstance(left.value, ast.Name) and isinstance(left.slice, ast.Constant) \
                and isinstance(right, (ast.List, ast.Tuple)):
            return Cond(var=left.value.id, field=str(left.slice.value),
                        cmp="in" if isinstance(op, ast.In) else "not_in",
                        values=[str(e.value) for e in right.elts if isinstance(e, ast.Constant)])
    # if v["field"]:  → exists;  if not v["field"]: → empty
    if isinstance(test, ast.Subscript) and isinstance(test.value, ast.Name) \
            and isinstance(test.slice, ast.Constant):
        return Cond(var=test.value.id, field=str(test.slice.value), cmp="exists")
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
            and isinstance(test.operand, ast.Subscript) and isinstance(test.operand.value, ast.Name) \
            and isinstance(test.operand.slice, ast.Constant):
        return Cond(var=test.operand.value.id, field=str(test.operand.slice.value), cmp="empty")
    raise CompileIssue(
        f"if 条件「{ast.unparse(test)}」不在支持范围。分支条件必须基于某一步的返回字段："
        "v[\"字段\"] ==/!= \"值\"、\"文字\" in v[\"字段\"]、v[\"字段\"] in (\"a\",\"b\")、"
        "v[\"字段\"]（存在）或 not v[\"字段\"]（为空）。复杂判断请先让一个 read/action 步"
        "returns 一个判定字段（read_spec 里写清判定规则），再对该字段分支"
    )


def _is_call_to(node: ast.AST, names: set[str]) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names


_COND_SEQ = [0]  # synthetic cond-scalar counter (module-level: names must be unique per program)


def _cond_or_scalar(test: ast.AST, out: list[Stmt]) -> Cond:
    """Compile an if-test: the direct Cond shapes when possible; otherwise fall back to a synthetic
    Compute scalar (`'True' if <expr> else 'False'`) + a self-field cond — so ANY safe_eval-able
    Python condition (numeric compares, membership, boolean logic) is expressible."""
    try:
        return _compile_cond(test)
    except CompileIssue:
        expr = _compute_expr(test)  # dialect-checked; raises CompileIssue with the fix hint
        _COND_SEQ[0] += 1
        var = f"cond_{_COND_SEQ[0]}"
        out.append(Compute(var=var, expr=f"'True' if ({expr}) else 'False'"))
        return Cond(var=var, field=var, cmp="==", value="True")


def _compile_block(body: list[ast.stmt], collects: Optional[dict] = None) -> list[Stmt]:
    collects = dict(collects or {})  # name → collect ast.Call, for `rows = collect(...)` then `for r in rows:`
    out: list[Stmt] = []
    for idx, stmt in enumerate(body):
        # bare call: run / finish
        if isinstance(stmt, ast.Expr) and _is_call_to(stmt.value, set(_RUN_FUNCS)):
            out.append(_compile_run(stmt.value, var=None))  # type: ignore[arg-type]
        elif isinstance(stmt, ast.Expr) and _is_call_to(stmt.value, {"finish"}):
            call = stmt.value  # type: ignore[assignment]
            if not call.args:
                raise CompileIssue("finish() 缺少答复文本参数")
            out.append(Finish(message=_const_str(call.args[0], "finish 文本")))
        elif isinstance(stmt, ast.Expr) and _is_call_to(stmt.value, {"subgoal"}):
            raise CompileIssue("subgoal() 只能作为 for 循环体的唯一语句（逐行子目标）")
        # assignment: run-with-var, saved collect, or compute
        elif isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                raise CompileIssue("赋值只支持单个变量名作为目标")
            var = stmt.targets[0].id
            if _is_call_to(stmt.value, set(_RUN_FUNCS)):
                out.append(_compile_run(stmt.value, var=var))  # type: ignore[arg-type]
            elif _is_call_to(stmt.value, {"collect"}):
                collects[var] = stmt.value  # `rows = collect(...)` → consumed by `for r in rows:`
            elif _is_call_to(stmt.value, {"subgoal", "finish"}):
                raise CompileIssue(f"{stmt.value.func.id}() 不能赋值给变量")  # type: ignore[union-attr]
            else:
                out.append(Compute(var=var, expr=_compute_expr(stmt.value)))
        # for row in collect(...):  /  for row in <saved collect name>:
        elif isinstance(stmt, ast.For):
            if isinstance(stmt.iter, ast.Name) and stmt.iter.id in collects:
                stmt = ast.For(target=stmt.target, iter=collects[stmt.iter.id],
                               body=stmt.body, orelse=stmt.orelse)
            if not isinstance(stmt.target, ast.Name) or not _is_call_to(stmt.iter, {"collect"}):
                raise CompileIssue("循环只支持 `for 变量 in collect(\"采集目标\", returns=[...]):`"
                                   "（或先 `rows = collect(...)` 再 `for 变量 in rows:`）")
            if stmt.orelse:
                raise CompileIssue("for-else 不支持")
            call = stmt.iter  # type: ignore[assignment]
            target_desc = _const_str(call.args[0], "collect 目标描述") if call.args else ""
            returns: list[str] = []
            limit = None
            into = ""
            for k in call.keywords:
                if k.arg == "returns" and isinstance(k.value, (ast.List, ast.Tuple)):
                    returns = [e.value for e in k.value.elts if isinstance(e, ast.Constant)]
                elif k.arg == "limit" and isinstance(k.value, ast.Constant):
                    limit = k.value.value if isinstance(k.value.value, int) else None
                elif k.arg == "into" and isinstance(k.value, ast.Constant):
                    into = str(k.value.value)
                elif k.arg == "read_spec":
                    # column-reading guidance folds into the collect target description
                    target_desc = f"{target_desc}（列读取说明：{_const_str(k.value, 'read_spec')}）"
                else:
                    raise CompileIssue(f"collect() 不支持关键字参数 {k.arg}（可用：returns/limit/into/read_spec）")
            # single-subgoal body → agentic per-row sub-goal
            if len(stmt.body) == 1 and isinstance(stmt.body[0], ast.Expr) \
                    and _is_call_to(stmt.body[0].value, {"subgoal"}):
                sg = stmt.body[0].value  # type: ignore[assignment]
                if not sg.args:
                    raise CompileIssue("subgoal() 缺少子目标文本")
                out.append(ForEach(var=stmt.target.id, target=target_desc, returns=returns,
                                   body_goal=_const_str(sg.args[0], "subgoal 文本"),
                                   into=into, limit=limit))
            else:
                out.append(ForEach(var=stmt.target.id, target=target_desc, returns=returns,
                                   body=_compile_block(stmt.body, collects), into=into, limit=limit))
        elif isinstance(stmt, ast.If):
            # Guard idiom `if C: continue` (rest of the loop body only runs when NOT C) →
            # If(C, then=[], otherwise=REST). The model writes this naturally for row filtering.
            if len(stmt.body) == 1 and isinstance(stmt.body[0], ast.Continue) and not stmt.orelse:
                cond = _cond_or_scalar(stmt.test, out)
                rest = _compile_block(body[idx + 1:], collects)
                out.append(If(cond=cond, then=[], otherwise=rest))
                return out
            cond = _cond_or_scalar(stmt.test, out)
            out.append(If(cond=cond,
                          then=_compile_block(stmt.body, collects),
                          otherwise=_compile_block(stmt.orelse, collects)))
        elif isinstance(stmt, ast.Pass):
            continue
        elif isinstance(stmt, ast.Continue):
            raise CompileIssue("continue 只支持 `if 条件: continue` 的守卫形式（且在循环体内）")
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            raise CompileIssue("不需要 import——navigate/filter/action/read/data_query/collect/subgoal/finish 都是内置的")
        elif isinstance(stmt, ast.FunctionDef):
            raise CompileIssue("原型不支持 def 定义函数——请把步骤内联（循环用 for + collect）")
        elif isinstance(stmt, (ast.While, ast.Try, ast.With, ast.AugAssign, ast.AnnAssign)):
            raise CompileIssue(f"不支持的语句类型 {type(stmt).__name__}——"
                               "只支持 里程碑调用/赋值/for-collect/if/finish")
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring / stray string → ignore
        else:
            raise CompileIssue(f"无法编译的语句：{ast.unparse(stmt)[:80]}")
    return out


def compile_python_plan(code: str, goal: str = "") -> Program:
    """Compile a restricted-Python plan to Program IR. Raises CompileIssue with author-facing
    feedback on the first offending construct (fed back through the decompose retry loop)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise CompileIssue(f"Python 语法错误（第 {e.lineno} 行）: {e.msg}") from e
    return Program(goal=goal, statements=_compile_block(tree.body))


# ── LLM front-end: goal → python code → compile → validate → retry ──────────

_CODE_BLOCK_RE = None  # lazy import re pattern


def _extract_code(text: str) -> str:
    global _CODE_BLOCK_RE
    import re
    if _CODE_BLOCK_RE is None:
        _CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)
    m = _CODE_BLOCK_RE.search(text)
    return (m.group(1) if m else text).strip()


def decompose_py(
    goal: str,
    *,
    knowledge: str = "",
    current_site: str = "",
    resolution=None,
    max_retries: int = 2,
    trace_sink: Optional[list] = None,
) -> Program:
    """Python-surface decompose: same LLM config as the JSON path (supervisor.decompose), plain-text
    output (a ```python block), compiled + validated with the same deterministic gates. Compile
    errors and validator issues feed the retry exactly like the JSON path's feedback loop."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from gui_agent.core.config import resolve_llm_config
    from gui_agent.core.router import intent_block
    from gui_agent.prompts import load_prompt_text

    from .validator import validate_program

    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
                     extra_body={"enable_thinking": False})
    system = load_prompt_text("task.orchestrator.pysurface")

    parts = [f"用户目标：\n{goal}\n"]
    ib = intent_block(resolution) if resolution is not None else None
    if ib is not None:
        parts.append(ib.content + "\n")
    if current_site:
        parts.append(f"当前站点：{current_site}\n")
    if knowledge:
        parts.append(f"应用知识：\n{knowledge}\n")
    base_human = "\n".join(parts)

    feedback = ""
    program = Program(goal=goal, statements=[])
    for attempt in range(max_retries + 1):
        msg = base_human + (f"\n上一版程序的问题（必须全部修正后重写完整程序）：\n{feedback}" if feedback else "")
        out = llm.invoke([SystemMessage(content=system), HumanMessage(content=msg)])
        code = _extract_code(out.content if isinstance(out.content, str) else str(out.content))
        if trace_sink is not None:
            trace_sink.append({"kind": "pysurface_code", "attempt": attempt, "code": code})
        try:
            program = compile_python_plan(code, goal)
        except CompileIssue as e:
            feedback = f"- [编译错误] {e}"
            if attempt < max_retries:
                print(f"  [PySurface] 编译失败，重试 ({attempt+1}/{max_retries}): {e}")
            continue
        issues = validate_program(program)
        if not issues:
            return program
        feedback = "\n".join(f"- {i}" for i in issues)
        if attempt < max_retries:
            print(f"  [PySurface] 校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{max_retries})...")
    return program
