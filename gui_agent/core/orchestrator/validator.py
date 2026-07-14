"""Program validator: deterministic shape/data-flow guards over a DSL Program.

`validate_program` owns the top-level scope walk and delegates independent rule families
to focused validator_* modules. It returns DSL-author-facing issues for repair retries
without depending on site-specific ground truth.
"""

from __future__ import annotations

import ast
import re

from .program import BARE_REF_RE, TEMPLATE_RE, Call, Compute, Finish, ForEach, FunctionDef, If, Program, Query, Run, RunLike, Stmt
from .primitives.safe_eval import FUNC_NAMES, dry_check_expr, normalize_compute_expr
from ._validator.data_query import check_foreach_data_query
from ._validator.issue import ALL_CODES, IssueList, ValidationIssue
from ._validator.retrieval import check_retrieval_retry_preserves_field
from .sql_utils import sql_identifier as _sql_identifier
from ._validator.sql import (
    aggregate_query_limits_after_aggregation as _aggregate_query_limits_after_aggregation,
    rank_query_drops_ties as _rank_query_drops_ties,
    sql_contains_template_ref as _sql_contains_template_ref,
    sql_cte_names as _sql_cte_names,
    sql_referenced_tables as _sql_referenced_tables,
    sql_uses_quoted_display_identifier as _sql_uses_quoted_display_identifier,
    sql_uses_schema_mapping_text as _sql_uses_schema_mapping_text,
    temporal_aggregate_without_row_limit as _temporal_aggregate_without_row_limit,
    temporal_limit_without_order as _temporal_limit_without_order,
)
from ._validator.url import check_foreach_url_policy, check_function_contract, template_fields_for_var


def _produces_result(run: Run) -> bool:
    # A run/read step may carry returns as an immediate read/acceptance shape without exporting
    # dataflow. Only var-bound returns enter scope; otherwise later if/finish/data_query cannot
    # address them deterministically. Function calls are stricter below because a declared function
    # return is explicitly a reusable structured value and must be bound by the caller.
    return bool(run.var) and (run.kind == "data_query" or bool(run.returns))

# navigate-shape intents: "go to / open / view / show / display the <X> (report|page|...)" with NO
# field/count/aggregation ask → reach+render a destination, NOT a structured-answer task. Mirrors
# synthesize_system.md's NAVIGATE test; without this, the verb "show"/"显示" alone (e.g. "Show the
# sales order report") wrongly trips NO_RESULT_SOURCE and pushes the plan to bolt on phantom returns.
_NAV_SHAPE_RE = re.compile(
    r"\b(go to|open|view|show|display|navigate to|查看|打开|进入|显示)\b.*"
    r"\b(report|page|section|dashboard|grid|list|settings|screen|tab|menu|报表|报告|页面|界面|设置)\b",
    re.IGNORECASE,
)
_RETRIEVE_KW_RE = re.compile(
    r"\b(how many|count|number of|total|top|most|least|rank|average|sum|which|who|whose|find|get|return)\b",
    re.IGNORECASE,
)

def _goal_expects_structured_answer(goal: str) -> bool:
    text = (goal or "").lower()
    # navigate-shape destination intent with no retrieve/aggregation keyword → just reach/render the
    # page; do NOT demand a returns/data_query result source. Do not treat every "of ..." as value
    # extraction: "Go to the list of completed orders" names a destination list, not an answer list.
    if _NAV_SHAPE_RE.search(text) and not _RETRIEVE_KW_RE.search(text):
        return False
    return bool(
        re.search(r"\b(how many|count|total|which|who|what|find|list|return|show)\b", text)
        # "number of X" = a count ask; but bare "number" is usually a NOUN (tracking/phone/order/
        # serial number) — matching it wrongly trips NO_RESULT_SOURCE on pure mutations like
        # "update order with the tracking number 123", forcing a phantom returns onto the write.
        or re.search(r"\bnumber of\b", text)
        or any(k in goal for k in ("多少", "几个", "几条", "总数", "数量", "哪些", "谁", "什么", "找出", "列出", "返回", "显示"))
    )

_MUTATE_GOAL_RE = re.compile(
    r"\b(update|change|reduce|increase|delete|remove|create|add|edit|set|disable|enable|assign|mark|rename|cancel|approve)\b"
)
_MUTATE_CN = ("更新", "修改", "删除", "移除", "创建", "新增", "新建", "设置", "改成", "改为",
              "降价", "涨价", "调价", "调整", "启用", "禁用", "标记", "重命名", "取消", "审核")
_MUTATE_STEP_CN_RE = re.compile(
    r"更新|修改|保存|删除|移除|设置|填(?:入|写)|改(?:成|为)|调价|降价|涨价|提交|创建|新增|启用|禁用"
    r"|update|save|delete|remove|set\b|edit|change|submit|create"
)


def _goal_is_mutation(goal: str) -> bool:
    text = (goal or "").lower()
    return bool(_MUTATE_GOAL_RE.search(text)) or any(k in (goal or "") for k in _MUTATE_CN)


def _has_mutation_step(stmts: list[Stmt], function_defs: dict | None = None) -> bool:
    """Any action-kind Run, or a foreach body_goal whose text carries a mutation verb (the sub-goal
    is re-decomposed at runtime, so its TEXT is what promises the mutation)."""
    for s in stmts:
        if isinstance(s, RunLike) and s.kind == "action":
            return True
        if isinstance(s, ForEach):
            if getattr(s, "body_goal", "") and _MUTATE_STEP_CN_RE.search(s.body_goal.lower()):
                return True
            if _has_mutation_step(s.body, function_defs):
                return True
        elif isinstance(s, If):
            if _has_mutation_step(s.then, function_defs) or _has_mutation_step(s.otherwise, function_defs):
                return True
    for fn in (function_defs or {}).values():
        if _has_mutation_step(fn.body, None):
            return True
    return False


def _has_result_source(stmts: list[Stmt], function_returns: dict[str, set[str]] | None = None) -> bool:
    function_returns = function_returns or {}
    for s in stmts:
        if isinstance(s, RunLike) and s.var and (s.returns or s.kind == "data_query"):
            return True
        if isinstance(s, Call) and s.var and function_returns.get(s.func):
            return True
        if isinstance(s, If) and (_has_result_source(s.then, function_returns) or _has_result_source(s.otherwise, function_returns)):
            return True
        if isinstance(s, ForEach) and _has_result_source(s.body, function_returns):
            return True
        if isinstance(s, ForEach) and (s.output_fields or s.row_fields or s.returns):
            return True
    return False


def _foreach_into_name(loop: ForEach) -> str:
    return (loop.into or f"{loop.var}s").strip()


def _pure_collection_foreaches(stmts: list[Stmt]) -> list[ForEach]:
    out: list[ForEach] = []
    for s in stmts:
        if isinstance(s, ForEach):
            if not s.body and not s.body_goal and (s.row_fields or s.returns):
                out.append(s)
            out.extend(_pure_collection_foreaches(s.body))
        elif isinstance(s, If):
            out.extend(_pure_collection_foreaches(s.then))
            out.extend(_pure_collection_foreaches(s.otherwise))
    return out


def _query_referenced_materialized_tables(stmts: list[Stmt]) -> set[str]:
    out: set[str] = set()
    for s in stmts:
        if isinstance(s, Query):
            out.update(_sql_referenced_tables(s.sql) - _sql_cte_names(s.sql))
        elif isinstance(s, If):
            out.update(_query_referenced_materialized_tables(s.then))
            out.update(_query_referenced_materialized_tables(s.otherwise))
        elif isinstance(s, ForEach):
            out.update(_query_referenced_materialized_tables(s.body))
    return {name.lower() for name in out}


def _iter_main_statements(stmts: list[Stmt]):
    """Yield main-program statements recursively, excluding function definitions."""
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, If):
            yield from _iter_main_statements(stmt.then)
            yield from _iter_main_statements(stmt.otherwise)
        elif isinstance(stmt, ForEach):
            yield from _iter_main_statements(stmt.body)


def _consumed_result_vars(stmts: list[Stmt]) -> set[str]:
    consumed: set[str] = set()
    for stmt in _iter_main_statements(stmts):
        texts: list[str] = []
        if isinstance(stmt, RunLike):
            texts.extend(
                [
                    stmt.name or "",
                    stmt.success_condition or "",
                    stmt.read_spec or "",
                    getattr(stmt, "sql", "") or "",
                ]
            )
            if isinstance(stmt, Query):
                consumed.update(_sql_referenced_tables(stmt.sql))
        elif isinstance(stmt, Compute):
            consumed.update(re.findall(r"\b[A-Za-z_]\w*\b", stmt.expr or ""))
        elif isinstance(stmt, Finish):
            texts.append(stmt.message or "")
        elif isinstance(stmt, Call):
            texts.extend(str(value) for value in (stmt.args or {}).values())
        elif isinstance(stmt, ForEach):
            if stmt.over:
                consumed.add(stmt.over)
            texts.extend([stmt.target or "", stmt.body_goal or "", stmt.member_desc or ""])
        elif isinstance(stmt, If) and stmt.cond is not None and stmt.cond.var:
            consumed.add(stmt.cond.var)
        for text in texts:
            consumed.update(match.group(1) for match in TEMPLATE_RE.finditer(text))
    return consumed


def _interactive_url_result_vars(stmts: list[Stmt]) -> set[str]:
    """Result vars whose URL-like field is used to open a single interactive target."""
    used: set[str] = set()
    for stmt in _iter_main_statements(stmts):
        if not isinstance(stmt, Run) or stmt.kind not in {"navigation", "action"}:
            continue
        text = f"{stmt.name}\n{stmt.success_condition}"
        for match in TEMPLATE_RE.finditer(text):
            field = match.group(2).strip().strip("'\"").lower()
            if any(token in field for token in ("url", "href", "link", "链接", "网址")):
                used.add(match.group(1))
    return used


# A step whose own acceptance says "nothing changes" is a no-op the LLM invented for flow
# control (185 sample:「保存当前行结果（逻辑上）: 无UI变化，仅用于流程控制」). Interactive steps
# must drive the UI; per-row accumulation is the runtime's job. Checker would spin on it.
_NOOP_STEP_RE = re.compile(
    r"无\s*UI\s*变化|无界面变化|不执行任何操作|无实际操作|仅用于流程控制|仅作流程控制|"
    r"（逻辑上）|\(逻辑上\)|no[- ]?op",
    re.IGNORECASE,
)


def validate_program(program: Program, *, resolution=None) -> list[ValidationIssue]:
    """Deterministic shape guards — the high-value ones for the read-driven data-flow patterns.

    A result-producing run must request fields + bind a var; an if must branch on a field a prior result returns; a
    precondition may only sit on a navigation step (it ensures a state, so on action/read/filter
    it would be wrongly accepted on frame 1); and every {var[field]} template ref (finish message
    OR — result-then-reference, rule 10 — a run's name/success_condition/read_spec) must resolve to a
    read field that is ALREADY PRODUCED on the same execution path. The scope check is path-
    sensitive, not a global symbol table: a ref is valid only if its read precedes it on every path
    reaching it, so forward refs (引用在前、读取在后) and cross-branch refs (一个分支读、另一个分支
    引用) are caught — at runtime env would be empty and the template silently fills "". After an
    if, a var is in scope downstream only if BOTH branches produced it AND they share a field
    (field INTERSECTION, not union — a var bound to disjoint fields per branch guarantees nothing).
    Returns human-readable issues (DSL-author-facing, no runtime internals) for one repair pass."""
    issues = IssueList()
    if not program.statements:
        return IssueList.one("EMPTY_PROGRAM", "程序为空：至少要有一个 run 步骤")
    function_defs = {fn.name: fn for fn in getattr(program, "functions", None) or []}
    function_returns = {name: {field for field in fn.returns if field} for name, fn in function_defs.items()}
    if _goal_expects_structured_answer(program.goal) and not _has_result_source(program.statements, function_returns):
        issues.add("NO_RESULT_SOURCE",
            "任务要求返回/查找/统计具体答案，但计划没有任何 returns 或 data_query 结果来源；"
            "不能用裸 finish 猜答案。请让产生结果的 navigation/filter/action 带 returns/read_spec，"
            "或在表格数据源准备好后使用 data_query，并让 finish 引用 {变量[字段]}。"
        )
    if _goal_is_mutation(program.goal) and not _has_mutation_step(program.statements, function_defs):
        # Offline 778 v4: a "Reduce the price ..." goal decomposed into collect+classify ONLY — a
        # foreach whose body_goal judged size-28 membership and returned action_url, with no step
        # anywhere that opens/updates/saves. Structurally legal, semantically verb-less: the plan
        # can only ever observe, never mutate.
        issues.add("MUTATE_GOAL_WITHOUT_ACTION",
            "任务是修改/写入类（改价/更新/删除/创建/设置…），但计划里没有任何 action 步、"
            "也没有含改动动词（更新/保存/删除/设置/填…）的 body_goal——整个计划只在采集/判断，"
            "永远不会执行任务要求的修改。请补上实际执行修改的 action 步骤（打开目标 → 修改字段 → 保存），"
            "多目标时放进 foreach body 逐个执行。"
        )

    consumed_result_vars = _consumed_result_vars(program.statements)
    interactive_url_result_vars = _interactive_url_result_vars(program.statements)
    for stmt in _iter_main_statements(program.statements):
        if (
            isinstance(stmt, Run)
            and stmt.kind == "filter"
            and stmt.returns
            and not stmt.target_values
        ):
            issues.add(
                "FILTER_RESULT_WITHOUT_TARGET_STATE",
                f"filter 步「{stmt.name}」会返回 {list(stmt.returns)}，但没有用 target_values "
                "声明完成后的完整筛选状态。运行时无法区分任务筛选、旧残留筛选和相邻搜索框，"
                "也无法可靠读取该结果。请按 {<语义筛选字段>: <精确值>} 填写 target_values；"
                "若要保留上游筛选，也必须一并列入。",
            )
        if (
            isinstance(stmt, Run)
            and stmt.kind == "action"
            and stmt.var
            and stmt.returns
            and stmt.var not in consumed_result_vars
        ):
            issues.add(
                "MUTATION_RESULT_UNUSED",
                f"action 步「{stmt.name}」声明了 var={stmt.var!r} / returns={list(stmt.returns)}，"
                "但后续 if/compute/query/run/finish 没有消费该结果。未消费的 save_status/status "
                "会迫使 success_condition 退化成『动作有响应』，即使保存了错误对象也可能通过。"
                "请直接删除该 action 的 var/returns/read_spec，并让 success_condition 验收 owner 身份"
                "与保存后的业务终态。禁止为了消耗这个状态而新增 if、compute、额外 read 或 option_exists；"
                "当前状态检查与幂等跳过由 interactive Run 在实时页面中处理，不属于 Program 控制流。",
                evidence=(stmt.var, tuple(stmt.returns)),
            )
        if isinstance(stmt, Query) and stmt.var:
            url_fields = {
                field
                for field in stmt.returns
                if any(token in field.lower() for token in ("url", "href", "link"))
            }
            if url_fields and stmt.var not in interactive_url_result_vars:
                issues.add(
                    "DATA_QUERY_URL_RESULT_UNUSED",
                    f"data_query 步「{stmt.name}」返回 URL/link 字段 {sorted(url_fields)}，"
                    "但后续步骤没有引用它。既然查询已经确定了目标入口，后续 navigation/action "
                    f"必须使用 {{{stmt.var}[字段]}}；否则请从 returns/SELECT 中删除该入口字段，"
                    "不要重新依赖视觉滚动或按名称猜目标。",
                    evidence=(stmt.var, tuple(sorted(url_fields))),
                )

    all_result_vars: set[str] = set()  # every result var anywhere — to spot botched bare refs
    rank_goal_text = (program.goal or "").lower()
    def _collect_result_vars(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, RunLike) and _produces_result(s):
                all_result_vars.add(s.var)
            elif isinstance(s, Call) and s.var and function_returns.get(s.func):
                all_result_vars.add(s.var)
            elif isinstance(s, If):
                _collect_result_vars(s.then)
                _collect_result_vars(s.otherwise)
            elif isinstance(s, ForEach):
                _collect_result_vars(s.body)
    _collect_result_vars(program.statements)
    for fn in getattr(program, "functions", None) or []:
        _collect_result_vars(fn.body)

    # Deterministic value binding: a Compute produces a SCALAR (e.g. new_price = current_price×0.865).
    # Its ONLY path onto the page is a later action whose NAME references it as bare `{var}` — the
    # runner (_render/_scalars) then types the EXACT computed value. If nothing downstream references
    # it, the computed value is DEAD and the consuming fill action has no concrete target, so the
    # action-level planner may otherwise invent one when the action only says "use the new value".
    # Require every Compute
    # var to be consumed — as `{var}` in some run/finish text, or as an identifier in a later Compute.
    _computes: list[tuple[str, str]] = []   # (var, expr) in program order
    _consumer_text: list[str] = []
    _consumed_names: set[str] = set()       # vars consumed by bare NAME (If cond.var — not {var} form)
    def _collect_computes(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, Compute) and s.var:
                _computes.append((s.var, s.expr or ""))
            elif isinstance(s, RunLike):
                _consumer_text.extend([
                    s.name or "",
                    s.success_condition or "",
                    s.read_spec or "",
                    getattr(s, "sql", "") or "",
                ])
            elif isinstance(s, Finish):
                _consumer_text.append(s.message or "")
            elif isinstance(s, Call):
                _consumer_text.extend(str(v) for v in (s.args or {}).values())
            if isinstance(s, If):
                if s.cond is not None:
                    if s.cond.var:
                        _consumed_names.add(s.cond.var)
                    _consumer_text.append(str(s.cond.value or ""))
                    _consumer_text.extend(str(v) for v in (s.cond.values or []))
                _collect_computes(s.then)
                _collect_computes(s.otherwise)
            elif isinstance(s, ForEach):
                _consumed_names.update(s.output_fields or [])
                if s.body_goal and not s.body:
                    _consumed_names.update(s.returns or [])
                _collect_computes(s.body)
    _collect_computes(program.statements)
    for fn in getattr(program, "functions", None) or []:
        # A Compute var named in the enclosing function's `returns` is consumed by the call
        # mechanism (the caller reads it via {callvar[field]}).
        _consumed_names.update(fn.returns or [])
        _collect_computes(fn.body)
    _text_blob = " ".join(_consumer_text)
    for _i, (_cvar, _) in enumerate(_computes):
        _in_text = ("{" + _cvar + "}") in _text_blob or _cvar in _consumed_names
        _in_other_expr = any(
            re.search(rf"\b{re.escape(_cvar)}\b", _e)
            for _j, (_v, _e) in enumerate(_computes) if _j != _i
        )
        if not _in_text and not _in_other_expr:
            severity = "error" if _goal_is_mutation(program.goal) else "warn"
            issues.add("COMPUTE_VAR_UNUSED",
                f"Compute 算出的「{_cvar}」没有被后续步骤消费——它既没有作为 {{{_cvar}}} "
                "出现在后续 run/finish/call 文本里，也没有被后续 compute、data_query SQL、"
                "function returns 或 foreach output_fields 使用。若它是要写入界面的值，"
                f"消费它的动作名必须写 {{{_cvar}}}（如『将价格更新为 {{{_cvar}}} 并保存』）；"
                "若它只是为了筛选/重命名多行结果，请删除这个 compute，改在 data_query 的 WHERE/SELECT AS 中完成。",
                severity=severity,
            )


    def _check_refs(text: str, where: str, scope: dict[str, set[str]]) -> None:
        # `scope` = result var -> returns, for result-producing runs already executed BEFORE this point.
        for m in TEMPLATE_RE.finditer(text or ""):
            var, field = m.group(1), m.group(2).strip().strip("'\"")
            if var not in scope:
                issues.add("TEMPLATE_VAR_NOT_IN_SCOPE", 
                    f"{where} 引用的 {{{var}[{field}]}} 中变量「{var}」在此处尚未产生"
                    f"（不是任何在它之前、且在当前执行路径上的返回值/data_query 步的 var；引用在前/读取在后/读取在另一分支都算）"
                    f"——指代落空；请先让前序步骤通过 returns 或 data_query 产生「{var}」并放在引用它之前（同一执行路径上），或删掉这个引用"
                )
            elif field not in scope[var]:
                issues.add("TEMPLATE_FIELD_NOT_IN_RETURNS", 
                    f"{where} 引用的字段「{var}[{field}]」不在该步骤返回的字段（returns）里"
                    f"——请改用它 returns 里已有的字段名，或在该步 returns 里补上「{field}」"
                )
        # botched bare {var}: a known read var written without [field] — neither resolves nor
        # matches the template, so the literal "{var}" leaks to the planner (回归 20260615_194320:
        # 「编辑机器人 {robot_name}」漏给了 planner). Force the {var[field]} form via repair.
        for m in BARE_REF_RE.finditer(text or ""):
            var = m.group(1)
            if var in all_result_vars:
                issues.add("TEMPLATE_BARE_VAR", 
                    f"{where} 用了裸 {{{var}}} 缺字段——「{var}」是某个返回值/data_query 步的结果变量，"
                    f"引用它的某个字段要写成 {{{var}[字段]}}（字段取自该步 returns）；裸 {{{var}}} 不会被填上值"
                )
        for m in re.finditer(r"\{([^{}]+)\}", text or ""):
            raw = m.group(0)
            expr = m.group(1).strip()
            if TEMPLATE_RE.fullmatch(raw) or BARE_REF_RE.fullmatch(raw):
                continue
            if any(var and var in expr for var in all_result_vars) or re.search(r"[()+\-*/]", expr):
                issues.add("TEMPLATE_UNSUPPORTED_EXPR", 
                    f"{where} 包含不支持的模板表达式 {{{expr}}}。"
                    "模板只支持 {变量[字段]} 引用；加减乘除、ABS/difference 等计算必须放进 data_query，"
                    "再让 finish 引用 data_query 返回字段。"
                )

    def _check_compute(s: Compute, scope: dict[str, set[str]], scalars: set[str]) -> None:
        # Compile-time enforcement of the runtime compute contract (mirrors runner._compute):
        # same expr normalization, dialect dry-run under the probe scope, then a name check
        # against the scalars/vars/fields actually visible at this point on this path.
        raw_expr = s.expr or ""
        quoted_templates = re.findall(
            r"(['\"])(?:(?!\1).)*\{\w+\[[^\[\]{}]+\]\}(?:(?!\1).)*\1",
            raw_expr,
        )
        if quoted_templates:
            issues.add("COMPUTE_UNSUPPORTED_EXPR",
                f"compute「{s.var} = {s.expr}」把模板引用放进了字符串字面量。"
                "compute 是 Python 表达式，不是模板/f-string；不要写 `'{q[count]} text'`。"
                "请改成拼接或格式化表达式，例如 `str(q['count']) + ' text'`，"
                "再让后续动作名用 `{计算变量}` 消费这个 compute 结果。"
            )
            return
        json_scalar = raw_expr.strip()
        if json_scalar in {"true", "false", "null"}:
            issues.add(
                "COMPUTE_UNSUPPORTED_EXPR",
                f"compute「{s.var} = {s.expr}」使用了 JSON 字面量 `{json_scalar}`。"
                "compute 是 Python 表达式：布尔/空值常量要写 `True`/`False`/`None`。"
                "但如果这个变量后续要写入标题、描述、消息或文本字段，不要用布尔值当占位；"
                "运行时才知道的分支必须先用 read/data_query 产出字段，再写三元表达式，"
                "例如 `str(q['count']) + ' text' if int(q['count']) > 0 else \"fallback text\"`。",
            )
            return
        expr = normalize_compute_expr(raw_expr)
        dialect_error = dry_check_expr(expr)
        if dialect_error:
            hint = ""
            if "Attribute" in dialect_error or "方法调用" in dialect_error:
                hint = "——行/结果字段要用下标 var['字段']（或裸字段名），不能用 var.字段 属性访问"
            elif "unterminated string literal" in dialect_error:
                hint = (
                    "——字符串字面量引号不合法；若文本里含单引号/apostrophe（如 don't），"
                    "请用双引号包住该字符串，或把单引号写成转义形式。"
                    "compute 表达式里拼接返回字段请写 `str(q['count']) + ' text'`，"
                    "不要把 `{q[count]}` 放进引号里当模板字符串"
                )
            elif (
                "NamedExpr" in dialect_error
                or "__import__" in raw_expr
                or "sqlalchemy" in raw_expr.lower()
                or "不允许的函数调用" in dialect_error
            ):
                hint = (
                    "——compute 不能执行外部代码、import、SQLAlchemy/engine/cursor，也不能用海象表达式绕过。"
                    "凡是查询 foreach 表、计数、筛选、取第一行，都必须改成 data_query；"
                    "compute 只接收 data_query 已返回的标量并做字符串/数值派生"
                )
            elif any(comp in dialect_error for comp in ("ListComp", "SetComp", "DictComp", "GeneratorExp")):
                # The compute engine rejecting a comprehension almost always means the model reached
                # for compute to AGGREGATE a collection (count/sum/filter rows of a foreach into
                # table) — compute is scalar-derivation only. Without naming the right tool the model
                # just tries another comprehension spelling and the retries never converge (webarena
                # 544: len([...]) → [...].__len__() → len([...]) across all 3 attempts → compile fail).
                hint = (
                    "——这是在对集合做聚合/计数（如统计某表里满足条件的行数）。compute 只做标量派生、"
                    "不支持列表/集合推导；请改用 data_query 查询 foreach 的 into 表，例如 "
                    "`SELECT COUNT(*) AS n FROM <into表> WHERE <条件>`，再由后续 compute/finish 引用该查询结果"
                )
            issues.add("COMPUTE_UNSUPPORTED_EXPR",
                f"compute「{s.var} = {s.expr}」运行时不支持：{dialect_error}{hint}。"
                "只允许：字符串方法、切片/下标、算术、比较、and/or、三元、re_sub/re_search/len/str/int/round/float/abs。"
            )
            return
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            issues.add("COMPUTE_UNSUPPORTED_EXPR", f"compute「{s.var} = {s.expr}」不是合法表达式：{exc.msg}")
            return
        legal = scalars | set(scope) | {f for fields in scope.values() for f in fields} | set(FUNC_NAMES)
        unknown = sorted({
            n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id not in legal
            and not n.id.endswith("_url")  # runtime-folded URL capability columns are legal
        })
        if unknown:
            issues.add("COMPUTE_UNKNOWN_NAME",
                f"compute「{s.var} = {s.expr}」引用的名字 {unknown} 在此处不可见——"
                "compute 作用域 = 函数参数、之前的 compute 变量、之前带 returns/data_query 步的 var 及其字段名"
                "（同一执行路径上）。请改用作用域内的名字，或先让前序步骤产出该字段。"
            )

    def _walk(stmts: list[Stmt], scope: dict[str, set[str]], scalars: set[str] | None = None, in_foreach_body: bool = False) -> None:
        # Sequential statements mutate `scope` in place; if-branches each get a copy, and only
        # vars produced on BOTH branches survive past the join (a var read in one branch isn't
        # guaranteed downstream). `scalars` tracks compute vars / function params the same way.
        scalars = set() if scalars is None else scalars
        for s in stmts:
            if isinstance(s, Compute):
                # Compile-time expr contract (dialect + scope names), then bind: `scalars` for the
                # compute-expr name check; scope[var]={var} keeps the ONE legal var[field] use of a
                # compute scalar — the SELF-FIELD cond (field == var) the Python-surface compiles
                # free-form `if <expr>:` into (the interpreter resolves it from the scalar scope).
                _check_compute(s, scope, scalars)
                if s.var:
                    scalars.add(s.var)
                    scope[s.var] = {s.var}
                continue
            if isinstance(s, RunLike):
                # precondition is a state to ENSURE (确保已到达/已进入某状态) — only meaningful on a
                # navigation step. On an action/read/filter it would be treated as already-satisfied
                # and accepted on frame 1, prematurely passing a step that must actually run.
                if isinstance(s, Run) and _NOOP_STEP_RE.search(f"{s.name} {s.success_condition}"):
                    issues.add("NOOP_FLOW_CONTROL_STEP",
                        f"步骤「{s.name}」自述无 UI 变化/仅用于流程控制——交互步必须驱动真实界面操作。"
                        "foreach 每行的读取结果会自动累积进 into 表，不需要任何「保存结果/流程控制」步；"
                        "请直接删除这一步。"
                    )
                if getattr(s, "precondition", False) and s.kind != "navigation":
                    issues.add("PRECONDITION_NOT_NAVIGATION", 
                        f"步骤「{s.name}」标了 precondition=true 但 run_kind={s.kind}——"
                        "前置状态保障只能是 navigation 步（确保已到达/已进入某状态），"
                        "不能标在 action/read/filter/data_query 上；请改成 navigation，或去掉 precondition"
                    )
                if s.kind == "read" and not s.returns:
                    issues.add("READ_MISSING_RETURNS", f"read 步「{s.name}」没有 returns 字段——read 必须指定要读取的字段")
                if s.kind == "read" and not s.var:
                    issues.add("READ_MISSING_VAR", f"read 步「{s.name}」没有绑定 var——读到的结果无法被后续引用")
                if s.kind == "data_query" and not s.returns:
                    issues.add("DATA_QUERY_MISSING_RETURNS", f"data_query 步「{s.name}」没有 returns 字段——data_query 必须指定要返回的字段")
                if s.kind == "data_query" and not s.var:
                    issues.add("DATA_QUERY_MISSING_VAR", f"data_query 步「{s.name}」没有绑定 var——查询结果无法被后续引用")
                if s.kind == "data_query" and not s.sql.strip():
                    issues.add("DATA_QUERY_MISSING_SQL", f"data_query 步「{s.name}」没有 sql——必须提供只读 SELECT/WITH SELECT")
                if s.kind == "data_query" and _sql_contains_template_ref(s.sql):
                    issues.add("DATA_QUERY_SQL_TEMPLATE_REF", 
                        f"data_query 步「{s.name}」的 SQL 包含模板表达式 {{...}}。"
                        "SQL 不会执行 {变量[字段]} 替换，也不能引用 action/read 的标量返回值；"
                        "data_query 只能查询当前结构化表格或 foreach into 表。若要做差值/比例/合计，"
                        "请先把相关行集 materialize 成表，再在同一个 SQL/CTE 里计算并输出字段。"
                    )
                if s.kind == "data_query":
                    scope_vars = {str(var).lower() for var in scope}
                    bad_var_tables = sorted((_sql_referenced_tables(s.sql) - _sql_cte_names(s.sql)) & scope_vars)
                    if bad_var_tables:
                        issues.add("DATA_QUERY_VAR_AS_TABLE", 
                            f"data_query 步「{s.name}」把前序结果变量当成 SQL 表名使用：{bad_var_tables}。"
                            "SQL 不能查询 read/action/data_query 的 var；只能查询当前表格 data/table_N/caption，"
                            "或 foreach 产出的 into 表。若要组合多个聚合结果，把这些聚合写进同一个 SQL/CTE。"
                        )
                if s.returns and s.kind not in {"read", "data_query"} and not s.read_spec.strip():
                    issues.add("RETURNS_WITHOUT_READ_SPEC", f"步骤「{s.name}」声明了 returns 但没有 read_spec——必须说明这些返回字段在完成帧上如何读取")
                if s.returns and s.kind != "data_query" and _run_looks_like_visual_row_aggregation(s):
                    issues.add("VISUAL_ROW_AGGREGATION", 
                        f"步骤「{s.name}」试图在 UI return/read_spec 中目测聚合表格前 N 行或多行数值。"
                        "表格求和/平均/差值/排名不能靠 action/read 手工相加；"
                        "请先用页面筛选/排序准备数据源，用 foreach body=[] 采集需要的网格列，"
                        "再用 data_query 对完整表做 SUM/AVG/COUNT/ABS 等计算。"
                    )
                if (
                    s.returns
                    and s.kind != "data_query"
                    # A step INSIDE a foreach body is the loop's per-row drill — it reads THIS row's
                    # detail (opened via {loop_var[...url]}), not "the currently-visible grid rows".
                    # foreach already guarantees the full row set is traversed, so this is exactly the
                    # sanctioned collection shape, not the visible-row shortcut this rule forbids. The
                    # rule's own text-heuristic can't tell them apart (the `{row[...]}` template makes
                    # the name contain "row"), so the exemption must be structural. Without it the
                    # correct plan for "aggregate a detail-only field then act" is impossible to ship
                    # — the drill is the only way to read a field absent from the grid (webarena 544).
                    and not in_foreach_body
                    and _goal_needs_table_analysis(program.goal)
                    and _run_looks_like_table_row_field_collection(s)
                ):
                    issues.add("TABLE_ROW_FIELD_COLLECTION",
                        f"步骤「{s.name}」把表格行字段挂在 {s.kind} returns 上读取。"
                        "聚合/排序/top-N 类任务不能让 filter/action/read 读取当前可见网格行字段；"
                        "这些字段应放在 foreach row_fields（旧计划 returns）中采集完整行集，然后用 data_query 分析。"
                    )
                if s.kind == "data_query" and _sql_uses_schema_mapping_text(s.sql):
                    issues.add("SQL_SCHEMA_MAPPING_TEXT", 
                        f"data_query 步「{s.name}」的 SQL 使用了 schema 显示映射文本（如 Header->column）。"
                        "SQL 只能写实际 normalized column identifiers，例如写 column，不要写 Header->column。"
                    )
                if s.kind == "data_query" and _sql_uses_quoted_display_identifier(s.sql):
                    issues.add("SQL_QUOTED_DISPLAY_IDENTIFIER", 
                        f"data_query 步「{s.name}」的 SQL 使用了带空格/标点的 quoted display label。"
                        "data_query 表格列已经归一化为 snake_case 标识符；SQL 应写 normalized column identifiers，"
                        "不要写 UI 表头/标签的双引号形式。"
                    )
                if s.kind == "data_query" and _rank_query_drops_ties(rank_goal_text, s):
                    issues.add("RANK_QUERY_DROPS_TIES", 
                        f"data_query 步「{s.name}」像是在回答聚合排名/第N多，但 SQL 使用 LIMIT/OFFSET 单行截断，"
                        "会丢掉并列项；请先 GROUP BY 计算 count，再用 DENSE_RANK() 或 HAVING count 返回该名次的所有并列结果"
                    )
                if s.kind == "data_query" and _aggregate_query_limits_after_aggregation(s.sql):
                    issues.add("AGGREGATE_LIMIT_AFTER_AGGREGATION", 
                        f"data_query 步「{s.name}」把 LIMIT 放在 SUM/AVG/COUNT 等聚合之后；"
                        "LIMIT 不会限制聚合输入行。若任务是最近/前 N 行的金额/数值聚合，"
                        "请先在 FROM 子查询里 ORDER BY/LIMIT 选出 N 行，再在外层 SUM/AVG/COUNT，"
                        "例如 SELECT SUM(amount_num) AS total FROM "
                        "(SELECT amount_num FROM rows ORDER BY date_ts DESC LIMIT N)。"
                    )
                if s.kind == "data_query" and _temporal_limit_without_order(rank_goal_text, s):
                    issues.add("TEMPORAL_LIMIT_WITHOUT_ORDER", 
                        f"data_query 步「{s.name}」在回答最近/最旧/last/latest/oldest 这类时间顺序任务时使用了 LIMIT，"
                        "但对应 SELECT 没有 ORDER BY；LIMIT 只能取任意/当前插入顺序行，不能代表最近/最旧。"
                        "请采集日期/时间列，并在子查询中 ORDER BY <date>_ts DESC/ASC 后再 LIMIT。"
                    )
                if s.kind == "data_query" and _temporal_aggregate_without_row_limit(rank_goal_text, s):
                    issues.add("TEMPORAL_AGGREGATE_WITHOUT_ROW_LIMIT", 
                        f"data_query 步「{s.name}」在回答最近/最旧/last/latest/oldest N 行的聚合任务时，"
                        "SQL 直接对整张表 SUM/AVG/COUNT，没有先按日期/时间 ORDER BY 后 LIMIT N。"
                        "请先用子查询或 CTE 选出目标 N 行，再聚合。"
                    )
                if (
                    s.kind == "data_query"
                    and s.var in interactive_url_result_vars
                    and re.search(r"\blimit\s+1\b", s.sql or "", flags=re.IGNORECASE)
                    and not re.search(r"\border\s+by\b", s.sql or "", flags=re.IGNORECASE)
                ):
                    issues.add(
                        "SINGLE_TARGET_LIMIT_HIDES_AMBIGUITY",
                        f"data_query 步「{s.name}」的结果 URL 被后续 navigation/action 当作单个目标入口，"
                        "但 SQL 用无 ORDER BY 的 LIMIT 1 任取一行；这不能证明 owner 唯一，会静默改错对象。"
                        "请让查询返回 COUNT(*) AS match_count，并仅在 match_count=1 时返回/使用 URL；"
                        "0 条或多条必须显式失败或继续消歧。只有按任务要求明确排序取 top-1 时才使用 "
                        "ORDER BY ... LIMIT 1。",
                        evidence=(s.var or "", s.sql or ""),
                    )
                # check this run's refs BEFORE binding its own var (a read can't reference its own
                # value — env[var] isn't set until the read completes)
                _check_refs(f"{s.name}\n{s.success_condition}\n{s.read_spec}", f"步骤「{s.name}」", scope)
                if _produces_result(s):
                    scope[s.var] = set(s.returns)
            elif isinstance(s, Call):
                fn = function_defs.get(s.func)
                if fn is None:
                    issues.add(
                        "CALL_FUNC_NOT_DEFINED",
                        f"call 调用了未定义的函数「{s.func}」；请先在 functions 中定义它，"
                        "或把这一步改成显式 run/compute/foreach。"
                    )
                for param, value in (s.args or {}).items():
                    _check_refs(str(value), f"call「{s.func}」参数「{param}」", scope)
                returns = function_returns.get(s.func, set())
                if returns and not s.var:
                    issues.add(
                        "CALL_RETURNS_WITHOUT_VAR",
                        f"call「{s.func}」调用的函数声明了 returns={sorted(returns)}，但 call 没有绑定 var；"
                        "这些返回字段无法被后续 if/finish/foreach/data_query 引用。请给 call 设置 var。"
                    )
                if s.var and returns:
                    scope[s.var] = set(returns)
            elif isinstance(s, Finish):
                _check_refs(s.message, "finish 模板", scope)
            elif isinstance(s, If):
                scalar_cond = s.cond.var in scalars and (
                    not s.cond.field or s.cond.field == s.cond.var
                )
                if s.cond.var not in scope and not scalar_cond:
                    issues.add("IF_COND_VAR_NOT_IN_SCOPE", 
                        f"if 条件引用的变量「{s.cond.var}」在此处尚未产生"
                        "（不是任何在它之前、且在当前执行路径上的返回值/data_query 步的 var）"
                        f"——请在这个 if 之前（同一执行路径上）让某个步骤通过 returns 或 data_query 产生「{s.cond.var}」"
                    )
                elif not scalar_cond and s.cond.field not in scope[s.cond.var]:
                    issues.add("IF_COND_FIELD_NOT_IN_RETURNS", 
                        f"if 条件字段「{s.cond.var}[{s.cond.field}]」不在该步骤返回的字段（returns）里"
                        f"——请把 cond_field 改成该步 returns 里已有的字段名，或在该步 returns 里补上「{s.cond.field}」"
                    )
                if s.cond.cmp in {"contains", "not_contains"} and not s.cond.value.strip():
                    issues.add("IF_COND_MISSING_VALUE", 
                        f"if 条件「{s.cond.var}[{s.cond.field}] {s.cond.cmp}」缺少 cond_value——"
                        "contains/not_contains 必须给出要匹配的文字"
                    )
                if s.cond.cmp in {"in", "not_in"} and not [v for v in s.cond.values if v.strip()]:
                    issues.add("IF_COND_MISSING_VALUES",
                        f"if 条件「{s.cond.var}[{s.cond.field}] {s.cond.cmp}」缺少 cond_values——"
                        "in/not_in 必须给出一个或多个候选值"
                    )
                # Empty-guard inversion: `count == '0'` means NOTHING matched, so the then-branch
                # must be the not-found exit and the work goes in else. A live 778 decompose put the
                # whole price-update pipeline under then and the "未找到" finish under else — at
                # runtime count='3' → else → finished "not found" with zero saves. Deterministic
                # shape check: guard==0 + work in then + else is only a finish ⇒ branches swapped
                # (and the symmetric `!= '0'` form).
                _zero = s.cond.value.strip() in {"0", "0条", "0 条"}
                if _zero and s.cond.cmp in {"==", "!="}:
                    work_branch, exit_branch = (s.then, s.otherwise) if s.cond.cmp == "==" else (s.otherwise, s.then)
                    _has_work = any(isinstance(x, (Run, ForEach, Call)) for x in work_branch)
                    _only_finish = bool(exit_branch) and all(isinstance(x, Finish) for x in exit_branch)
                    if _has_work and _only_finish:
                        issues.add("IF_EMPTY_GUARD_INVERTED",
                            f"if 条件「{s.cond.var}[{s.cond.field}] {s.cond.cmp} '0'」是空集守卫，但分支放反了："
                            f"{'then' if s.cond.cmp == '==' else 'else'} 分支（={s.cond.cmp} '0'＝没找到）装着主要工作，"
                            f"而另一分支只有 finish——运行时一旦找到记录（count≠0）会直接走 finish 报「未找到」、跳过全部工作。"
                            "请交换两个分支：命中 0 的那支放「未找到」finish，另一支放实际工作"
                        )
                then_scope, else_scope = dict(scope), dict(scope)
                _walk(s.then, then_scope, set(scalars), in_foreach_body)
                _walk(s.otherwise, else_scope, set(scalars), in_foreach_body)
                # join: a var is in scope downstream only if BOTH branches produced it AND they
                # share a field — fields must INTERSECT, not union. (one branch returns 名称, the
                # other 编号 → no field is guaranteed on every path → drop the var; a later
                # {var[名称]} would silently fill "" on the 编号 path. union wrongly let it pass.)
                for k in set(then_scope) | set(else_scope):
                    common = then_scope.get(k, set()) & else_scope.get(k, set())
                    if common:
                        scope[k] = common
                    else:
                        scope.pop(k, None)
            elif isinstance(s, ForEach):
                # `over` is optional: non-empty = points to an env var with .rows; empty = browser
                # collect_fn path (target/returns used to collect rows directly via DOM/AX tree).
                # When over is provided, validate it resolves to a read var in scope.
                if s.over and s.over not in scope:
                    issues.add("FOREACH_OVER_NOT_IN_SCOPE", 
                        f"foreach 的 over「{s.over}」在此处尚未产生"
                        "（不是任何在它之前、且在当前执行路径上的 read 步的 var）；"
                        f"请在这个 foreach 之前让某个 read 步产生「{s.over}」"
                    )
                if not s.var:
                    issues.add("FOREACH_MISSING_LOOP_VAR", "foreach 缺少循环变量名（loop_var）——body 需要用 {循环变量[字段]} 引用当前行")
                agentic_body_goal = bool(s.body_goal) and not s.body
                if agentic_body_goal:
                    # Agentic per-row sub-goal (body_goal WITHOUT body): decomposed fresh at runtime,
                    # so it must template the row (else every row runs identically — live 215344) and
                    # declare what to return per row. New programs should split row_fields (current
                    # grid/list row inputs) from output_fields (per-row sub-goal outputs); legacy
                    # body_goal+returns still means output_fields. (body_goal WITH body = a docstring
                    # on a templated sub-function — allowed; the body itself carries the steps.)
                    if not (s.output_fields or s.returns):
                        issues.add("FOREACH_BODY_GOAL_MISSING_RETURNS", f"foreach（循环变量「{s.var}」）的 body_goal 必须配 output_fields（旧计划可用 returns）——"
                                   "声明每行子目标要产出的字段（如 ['material']），否则结果汇不进 into 表供后续查询")
                    if ("{%s[" % s.var) not in s.body_goal:
                        issues.add("FOREACH_BODY_GOAL_NO_ROW_TEMPLATE", f"foreach 的 body_goal 必须**字面**引用循环变量模板 "
                                   f"`{{{s.var}[字段]}}`（如 `{{{s.var}[Name]}}`），运行时才按行代入；否则每行子目标都一样、"
                                   "只命中第一个（live 215344：两次都搜同一个名字）")
                elif not s.body and not (s.row_fields or s.returns):
                    issues.add("FOREACH_EMPTY_BODY_NO_RETURNS", f"foreach（循环变量「{s.var}」）的 body 为空且未设置 row_fields/returns——"
                                  "若目标列已在网格里，在 foreach 上设 row_fields（旧计划可用 returns，系统自动从网格直取这些字段）；"
                                  "若需逐行钻详情，在 body 里添加打开详情的步骤")
                # body runs in a copied scope with the loop var bound to the over-read's row fields (if any),
                # so {loop_var[field]} resolves. The loop's materialized `into` table is queried by a
                # following data_query via SQL table name (not a {var[field]} template), so it isn't
                # added to template scope here.
                body_scope = dict(scope)
                if s.over:
                    body_scope[s.var] = set(scope.get(s.over, set())) | set(s.row_fields)
                else:
                    # Browser collect_fn path. New programs use row_fields for row inputs; old
                    # non-body_goal programs used returns. A legacy body_goal without row_fields
                    # derives row inputs from the literal templates it wrote.
                    if s.row_fields:
                        body_scope[s.var] = set(s.row_fields)
                    elif agentic_body_goal:
                        body_scope[s.var] = template_fields_for_var(s.body_goal, s.var)
                    else:
                        body_scope[s.var] = set(s.returns) if s.returns else set()
                check_foreach_url_policy(s, body_scope.get(s.var, set()), function_defs, issues)
                _walk(s.body, body_scope, set(scalars), in_foreach_body=True)

    _walk(program.statements, {}, set())
    # Function bodies are validated under the SAME rules, each starting from an empty result-var
    # scope (params/computes are scalars, not result vars). This is what makes IF_COND_VAR_NOT_IN_SCOPE
    # catch a self-first resolution that degenerates to hardcoded-always-parent: an `if self_d[...]`
    # whose `self_d` isn't bound by a preceding read/call IN THE FUNCTION → dead conditional → every
    # row falls to else → always-parent (the "编排逻辑写死" root cause). Without walking functions, the
    # if inside resolve_product_material was never checked.
    for fn in getattr(program, "functions", None) or []:
        # A DSL function is a per-row/per-entity subroutine — the preferred drill shape is a foreach
        # body doing `op=call drill_fn(row)` (the 185 material lineage). Its body runs逐行, not a
        # top-level read of the visible grid for aggregation, so it gets the SAME foreach-body
        # exemption from TABLE_ROW_FIELD_COLLECTION; otherwise a factored drill returning ≥2 fields
        # (or a marker field) trips the rule while the identical inline foreach body is exempt.
        _walk(fn.body, {}, set(fn.params or []), in_foreach_body=True)  # params = entry scalars
        check_function_contract(fn, function_defs, function_returns, issues)
    check_foreach_data_query(
        program.statements,
        issues,
        function_returns,
        goal_text=program.goal or "",
    )
    referenced_tables = _query_referenced_materialized_tables(program.statements)
    for loop in _pure_collection_foreaches(program.statements):
        into_name = _foreach_into_name(loop)
        if into_name and into_name.lower() not in referenced_tables:
            issues.add(
                "FOREACH_COLLECTION_UNUSED",
                f"foreach「{loop.target or into_name}」是 body=[] 的纯采集，产出的表「{into_name}」"
                "没有被后续 data_query 查询。纯采集本身不打开详情、不点击、不修改，也不会把 row 暴露给后续"
                "navigation/action；如果要选最近/最旧/top-N，请在 foreach 后用 data_query 查询该 into 表，"
                "再用查询结果中的 URL/ID 打开目标；如果只是依赖 UI 排序点第一行，请删除这个 foreach，"
                "并把后续导航明确写成打开当前已筛选排序列表的第一行。"
            )
    check_retrieval_retry_preserves_field(program.statements, issues)
    return issues
_VISUAL_ROW_AGG_RE = re.compile(
    r"("
    r"(?:sum|total|average|avg|difference|add up|aggregate).{0,80}"
    r"(?:first|top|last|latest|recent|oldest|前|最近|最后|最新|最旧|最早)\s*\d+"
    r"|(?:first|top|last|latest|recent|oldest)\s*\d+.{0,80}"
    r"(?:sum|total|average|avg|difference|add up|aggregate)"
    r"|(?:前|最近|最后|最新|最旧|最早)\s*\d+\s*(?:行|条|笔|个|rows?|records?|orders?).{0,80}"
    r"(?:总和|合计|求和|相加|平均|差值|聚合|sum|total|average|avg|difference)"
    r"|(?:总和|合计|求和|相加|平均|差值|聚合).{0,80}"
    r"(?:前|最近|最后|最新|最旧|最早)\s*\d+\s*(?:行|条|笔|个|rows?|records?|orders?)"
    r")",
    flags=re.IGNORECASE,
)

def _run_looks_like_visual_row_aggregation(run: Run) -> bool:
    """Catch plans that ask a UI run to manually add table rows instead of using data_query."""
    text = f"{run.name}\n{run.success_condition}\n{run.read_spec}\n{' '.join(run.returns or [])}".lower()
    if not _VISUAL_ROW_AGG_RE.search(text):
        return False
    return bool(
        re.search(r"\b(row|rows|record|records|order|orders|table|grid)\b|行|条|笔|表格|网格", text)
    )

def _goal_needs_table_analysis(goal: str) -> bool:
    text = (goal or "").lower()
    return bool(
        re.search(
            r"\b(count|sum|total|average|avg|top|most|least|rank|second|third|fourth|fifth|last|latest|recent|oldest|difference)\b",
            text,
        )
        or re.search(r"总数|数量|求和|总额|平均|最多|最少|排名|第[二三四五]|最近|最后|最新|最旧|最早|差值|差异", goal or "")
    )

def _run_looks_like_table_row_field_collection(run: Run) -> bool:
    """Non-data runs should prepare a table source, not return per-row grid fields for analysis."""
    text = f"{run.name}\n{run.success_condition}\n{run.read_spec}".lower()
    if not re.search(r"\b(row|rows|record|records|table|grid|visible)\b|行|条|表格|网格|可见", text):
        return False
    returns = [str(ret or "").strip().lower() for ret in (run.returns or [])]
    if not returns:
        return False
    if len(returns) == 1 and re.search(r"(?:record|row|result|match).*count|records?_found|count|total_records|数量|总数", returns[0]):
        return False
    row_value_markers = (
        "date", "time", "amount", "price", "total", "grand", "status", "customer", "email",
        "sku", "name", "qty", "quantity", "日期", "时间", "金额", "总额", "状态", "客户", "邮箱", "数量",
    )
    return len(returns) >= 2 or any(any(marker in ret for marker in row_value_markers) for ret in returns)
