"""Program validator: deterministic shape/data-flow guards over a DSL Program.

Split out of decomposer.py (the decompose path imports validate_program back) so the
~40 semantic rules live in one governable place. `validate_program` returns DSL-author-
facing issues for one repair-retry pass.

Phase A: pure extraction, behavior identical to the in-decomposer version. (ValidationIssue
codes/severity are layered on in a follow-up so each rule becomes measurable.)
"""

from __future__ import annotations

import re

from .program import BARE_REF_RE, TEMPLATE_RE, Finish, ForEach, If, Program, Run, Stmt

# Severity is metadata for governance/measurement; today every rule equally triggers the one
# repair-retry, so all issues are "error". Differentiating (e.g. "warn" advisory rules) is a
# later behavior change — keep it uniform here to stay a pure refactor.
_SEVERITIES = frozenset({"error", "warn"})


class ValidationIssue(str):
    """A single validator finding. IS its DSL-author-facing message (a `str` subclass) so the
    decompose retry loop can feed it straight back to the LLM and existing substring/`== []`
    assertions keep working unchanged — while carrying structured metadata (`code`, `severity`,
    `evidence`) so rules become countable, filterable, and individually testable.

    The string value == `message_for_llm`. `code` is a stable identifier per rule; `evidence`
    is an optional structured payload (the offending step name / SQL / fields) for telemetry.
    """

    code: str
    severity: str
    evidence: tuple

    def __new__(cls, code: str, message_for_llm: str, *, severity: str = "error", evidence=()) -> "ValidationIssue":
        if severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        obj = super().__new__(cls, message_for_llm)
        obj.code = code
        obj.severity = severity
        obj.evidence = tuple(evidence)
        return obj

    @property
    def message_for_llm(self) -> str:
        return str(self)


class IssueList(list):
    """A `list[ValidationIssue]` with an `.add(code, message, ...)` collector so each rule site
    names its code in one place. Subclasses `list`, so it stays compatible everywhere the old
    `list[str]` flowed (iteration, `== []`, prompt joining)."""

    def add(self, code: str, message: str, *, severity: str = "error", evidence=()) -> None:
        self.append(ValidationIssue(code, message, severity=severity, evidence=evidence))

    @classmethod
    def one(cls, code: str, message: str, *, severity: str = "error", evidence=()) -> "IssueList":
        lst = cls()
        lst.add(code, message, severity=severity, evidence=evidence)
        return lst


# Canonical registry of every code validate_program can emit — the single source of truth that
# makes the rule set governable: tests assert (a) the codes emitted in this module == ALL_CODES
# (no drift), and (b) each code has at least one triggering sample (no dead rule). Add a code here
# in the same change that adds its `issues.add(...)` site.
ALL_CODES: frozenset[str] = frozenset({
    # program shape / result source
    "EMPTY_PROGRAM",
    "NO_RESULT_SOURCE",
    # {var[field]} template references (finish + result-then-reference run text)
    "TEMPLATE_VAR_NOT_IN_SCOPE",
    "TEMPLATE_FIELD_NOT_IN_RETURNS",
    "TEMPLATE_BARE_VAR",
    "TEMPLATE_UNSUPPORTED_EXPR",
    # per-run shape
    "PRECONDITION_NOT_NAVIGATION",
    "READ_MISSING_RETURNS",
    "READ_MISSING_VAR",
    "DATA_QUERY_MISSING_RETURNS",
    "DATA_QUERY_MISSING_VAR",
    "DATA_QUERY_MISSING_SQL",
    "DATA_QUERY_SQL_TEMPLATE_REF",
    "DATA_QUERY_VAR_AS_TABLE",
    "RETURNS_WITHOUT_VAR",
    "RETURNS_WITHOUT_READ_SPEC",
    # table aggregation must go through data_query, not UI eyeballing
    "VISUAL_ROW_AGGREGATION",
    "TABLE_ROW_FIELD_COLLECTION",
    # data_query SQL hygiene
    "SQL_SCHEMA_MAPPING_TEXT",
    "SQL_QUOTED_DISPLAY_IDENTIFIER",
    "RANK_QUERY_DROPS_TIES",
    "AGGREGATE_LIMIT_AFTER_AGGREGATION",
    "TEMPORAL_LIMIT_WITHOUT_ORDER",
    "TEMPORAL_AGGREGATE_WITHOUT_ROW_LIMIT",
    # if-condition shape
    "IF_COND_VAR_NOT_IN_SCOPE",
    "IF_COND_FIELD_NOT_IN_RETURNS",
    "IF_COND_MISSING_VALUE",
    "IF_COND_MISSING_VALUES",
    # foreach shape
    "FOREACH_OVER_NOT_IN_SCOPE",
    "FOREACH_MISSING_LOOP_VAR",
    "FOREACH_EMPTY_BODY_NO_RETURNS",
    # retrieval exact->fuzzy retry must keep the same field
    "RETRIEVAL_RETRY_DROPS_FIELD",
    # foreach / data_query field provenance
    "FOREACH_DQ_ROW_FIELD_MISSING",
    "FOREACH_DQ_UNKNOWN_TABLE",
    "FOREACH_DQ_GRID_FIELD_MISSING",
    "FOREACH_DQ_DETAIL_FIELD_MISSING",
    "FOREACH_DQ_POST_FOREACH_FIELD_MISSING",
})


def _produces_result(run: Run) -> bool:
    return bool(run.var) and (run.kind == "data_query" or bool(run.returns))

def _goal_expects_structured_answer(goal: str) -> bool:
    text = (goal or "").lower()
    return bool(
        re.search(r"\b(how many|count|number|total|which|who|what|find|list|return|show)\b", text)
        or any(k in goal for k in ("多少", "几个", "几条", "总数", "数量", "哪些", "谁", "什么", "找出", "列出", "返回", "显示"))
    )

def _has_result_source(stmts: list[Stmt]) -> bool:
    for s in stmts:
        if isinstance(s, Run) and (s.returns or s.kind == "data_query"):
            return True
        if isinstance(s, If) and (_has_result_source(s.then) or _has_result_source(s.otherwise)):
            return True
        if isinstance(s, ForEach) and _has_result_source(s.body):
            return True
    return False

def validate_program(program: Program) -> list[ValidationIssue]:
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
    if _goal_expects_structured_answer(program.goal) and not _has_result_source(program.statements):
        issues.add("NO_RESULT_SOURCE", 
            "任务要求返回/查找/统计具体答案，但计划没有任何 returns 或 data_query 结果来源；"
            "不能用裸 finish 猜答案。请让产生结果的 navigation/filter/action 带 returns/read_spec，"
            "或在表格数据源准备好后使用 data_query，并让 finish 引用 {变量[字段]}。"
        )

    all_result_vars: set[str] = set()  # every result var anywhere — to spot botched bare refs
    rank_goal_text = (program.goal or "").lower()

    def _collect_result_vars(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, Run) and _produces_result(s):
                all_result_vars.add(s.var)
            elif isinstance(s, If):
                _collect_result_vars(s.then)
                _collect_result_vars(s.otherwise)
            elif isinstance(s, ForEach):
                _collect_result_vars(s.body)
    _collect_result_vars(program.statements)


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

    def _walk(stmts: list[Stmt], scope: dict[str, set[str]]) -> None:
        # Sequential statements mutate `scope` in place; if-branches each get a copy, and only
        # vars produced on BOTH branches survive past the join (a var read in one branch isn't
        # guaranteed downstream).
        for s in stmts:
            if isinstance(s, Run):
                # precondition is a state to ENSURE (确保已到达/已进入某状态) — only meaningful on a
                # navigation step. On an action/read/filter it would be treated as already-satisfied
                # and accepted on frame 1, prematurely passing a step that must actually run.
                if s.precondition and s.kind != "navigation":
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
                if s.returns and s.kind != "data_query" and not s.var:
                    issues.add("RETURNS_WITHOUT_VAR", f"步骤「{s.name}」声明了 returns 但没有绑定 var——返回值无法被后续 if/finish/foreach 引用")
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
                    and _goal_needs_table_analysis(program.goal)
                    and _run_looks_like_table_row_field_collection(s)
                ):
                    issues.add("TABLE_ROW_FIELD_COLLECTION", 
                        f"步骤「{s.name}」把表格行字段挂在 {s.kind} returns 上读取。"
                        "聚合/排序/top-N 类任务不能让 filter/action/read 读取当前可见网格行字段；"
                        "这些字段应放在 foreach returns 中采集完整行集，然后用 data_query 分析。"
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
                # check this run's refs BEFORE binding its own var (a read can't reference its own
                # value — env[var] isn't set until the read completes)
                _check_refs(f"{s.name}\n{s.success_condition}\n{s.read_spec}", f"步骤「{s.name}」", scope)
                if _produces_result(s):
                    scope[s.var] = set(s.returns)
            elif isinstance(s, Finish):
                _check_refs(s.message, "finish 模板", scope)
            elif isinstance(s, If):
                if s.cond.var not in scope:
                    issues.add("IF_COND_VAR_NOT_IN_SCOPE", 
                        f"if 条件引用的变量「{s.cond.var}」在此处尚未产生"
                        "（不是任何在它之前、且在当前执行路径上的返回值/data_query 步的 var）"
                        f"——请在这个 if 之前（同一执行路径上）让某个步骤通过 returns 或 data_query 产生「{s.cond.var}」"
                    )
                elif s.cond.field not in scope[s.cond.var]:
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
                then_scope, else_scope = dict(scope), dict(scope)
                _walk(s.then, then_scope)
                _walk(s.otherwise, else_scope)
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
                if not s.body and not s.returns:
                    issues.add("FOREACH_EMPTY_BODY_NO_RETURNS", f"foreach（循环变量「{s.var}」）的 body 为空且未设置 returns——"
                                  "若目标列已在网格里，在 foreach 上设 returns（系统自动从网格直取这些字段）；"
                                  "若需逐行钻详情，在 body 里添加打开详情的步骤")
                # body runs in a copied scope with the loop var bound to the over-read's row fields (if any),
                # so {loop_var[field]} resolves. The loop's materialized `into` table is queried by a
                # following data_query via SQL table name (not a {var[field]} template), so it isn't
                # added to template scope here.
                body_scope = dict(scope)
                if s.over:
                    body_scope[s.var] = set(scope.get(s.over, set()))
                else:
                    # new-style foreach: loop var fields come from foreach.returns (collect_fn provides them)
                    body_scope[s.var] = set(s.returns) if s.returns else set()
                _walk(s.body, body_scope)

    _walk(program.statements, {})
    _check_foreach_data_query(program.statements, issues)
    _check_retrieval_retry_preserves_field(program.statements, issues)
    return issues

_RETRIEVAL_FIELD_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{0,30}|[\u4e00-\u9fff]{1,12})\s*"
    r"(?:列|字段|输入框|筛选框|搜索框|下拉框|column|field|filter|name|名)",
    re.IGNORECASE,
)

_RETRIEVAL_FIELD_EQ_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/-]{0,30}|[\u4e00-\u9fff]{1,12})\s*(?:=|:|：)",
    re.IGNORECASE,
)

_RETRIEVAL_RETRY_CUE_RE = re.compile(
    r"0\s*条|无结果|空结果|关键词|模糊|放宽|包含|contains|fuzzy|broaden|partial",
    re.IGNORECASE,
)

_RETRIEVAL_ACTION_CUE_RE = re.compile(
    r"搜索|筛选|检索|查找|重筛|重搜|filter|search|query",
    re.IGNORECASE,
)

_RETRIEVAL_FIELD_STOPWORDS = {
    "当前", "目标", "搜索", "筛选", "关键", "关键词", "记录", "列表", "结果", "相关",
    "使用", "输入", "提交", "页面", "the", "same", "target", "filter", "search",
}

_RETRIEVAL_FIELD_PREFIX_RE = re.compile(
    r"^(?:先用|使用|用|在|按|以|将|把|从|当前|目标|same|target|in|on|by|using)\s*",
    re.IGNORECASE,
)

def _normalize_retrieval_field(raw: str) -> str:
    field = re.sub(r"\s+", " ", str(raw or "").strip(" '\"「」『』“”‘’")).strip()
    previous = None
    while field and field != previous:
        previous = field
        field = _RETRIEVAL_FIELD_PREFIX_RE.sub("", field).strip()
    lowered = field.lower()
    if not field or lowered in _RETRIEVAL_FIELD_STOPWORDS:
        return ""
    return lowered

def _retrieval_field_aliases(field: str) -> set[str]:
    norm = _normalize_retrieval_field(field)
    aliases = {norm} if norm else set()
    bilingual = {
        "产品": {"product", "产品", "商品"},
        "商品": {"product", "产品", "商品"},
        "product": {"product", "产品", "商品"},
        "客户": {"customer", "客户"},
        "customer": {"customer", "客户"},
        "昵称": {"nickname", "昵称"},
        "nickname": {"nickname", "昵称"},
        "标题": {"title", "标题"},
        "title": {"title", "标题"},
        "状态": {"status", "状态"},
        "status": {"status", "状态"},
    }
    aliases.update(bilingual.get(norm, set()))
    return {_normalize_retrieval_field(alias) for alias in aliases if _normalize_retrieval_field(alias)}

def _extract_retrieval_fields(text: str) -> list[str]:
    fields: list[str] = []
    for pattern in (_RETRIEVAL_FIELD_RE, _RETRIEVAL_FIELD_EQ_RE):
        for raw in pattern.findall(text or ""):
            field = _normalize_retrieval_field(raw)
            if field and field not in fields:
                fields.append(field)
    return fields

def _retrieval_fields_overlap(left: list[str], right: list[str]) -> bool:
    for a in left:
        aliases = _retrieval_field_aliases(a)
        if not aliases:
            continue
        for b in right:
            if aliases & _retrieval_field_aliases(b):
                return True
    return False

def _flatten_branch_runs(stmts: list[Stmt]) -> list[Run]:
    out: list[Run] = []
    for item in stmts:
        if isinstance(item, Run):
            out.append(item)
        elif isinstance(item, If):
            out.extend(_flatten_branch_runs(item.then))
            out.extend(_flatten_branch_runs(item.otherwise))
        elif isinstance(item, ForEach):
            out.extend(_flatten_branch_runs(item.body))
    return out

def _looks_like_fuzzy_retry(text: str) -> bool:
    return bool(_RETRIEVAL_RETRY_CUE_RE.search(text or "") and _RETRIEVAL_ACTION_CUE_RE.search(text or ""))

def _check_retrieval_retry_preserves_field(stmts: list[Stmt], issues: IssueList) -> None:
    """Exact->fuzzy retry branches must keep the same target field/column.

    This is a generic data-source guard, not a site rule: when a search/filter step already
    identifies the target field, an empty-result retry that only says "use keyword K again" lets
    the planner substitute any visible input. Keep the field in the branch so DOM-level field
    guards can enforce it.
    """

    def _walk_seq(seq: list[Stmt]) -> None:
        previous_fields: list[str] = []
        previous_label = ""
        for item in seq:
            if isinstance(item, Run):
                text = f"{item.name}\n{item.success_condition}\n{item.read_spec}"
                field_text = f"{item.name}\n{item.success_condition}"
                fields = _extract_retrieval_fields(field_text)
                if item.kind in {"filter", "action"} and fields and _RETRIEVAL_ACTION_CUE_RE.search(text):
                    previous_fields = fields
                    previous_label = item.name
                else:
                    previous_fields = []
                    previous_label = ""
                continue
            if isinstance(item, If):
                if previous_fields:
                    for branch_name, branch in (("then", item.then), ("otherwise", item.otherwise)):
                        for run in _flatten_branch_runs(branch):
                            if run.kind not in {"filter", "action"}:
                                continue
                            text = f"{run.name}\n{run.success_condition}\n{run.read_spec}"
                            field_text = f"{run.name}\n{run.success_condition}"
                            if not _looks_like_fuzzy_retry(text):
                                continue
                            fields = _extract_retrieval_fields(field_text)
                            if not _retrieval_fields_overlap(previous_fields, fields):
                                issues.add("RETRIEVAL_RETRY_DROPS_FIELD", 
                                    f"if 分支 {branch_name} 的检索回退步骤「{run.name}」没有保留上一检索步骤"
                                    f"「{previous_label}」的目标字段/列 {previous_fields}。"
                                    "精确→关键词/模糊重试必须继续点名同一个字段/列（例如「在同一字段输入关键词并提交」），"
                                    "不能退化成泛关键词搜索，否则执行层可能把值填进相近但错误的字段。"
                                )
                _walk_seq(item.then)
                _walk_seq(item.otherwise)
                previous_fields = []
                previous_label = ""
            elif isinstance(item, ForEach):
                _walk_seq(item.body)
                previous_fields = []
                previous_label = ""

    _walk_seq(stmts)

_SQL_NON_FIELD_TOKENS = {
    "abs", "all", "and", "as", "asc", "avg", "between", "by", "case", "cast", "count", "dense_rank",
    "coalesce", "desc", "distinct", "else", "end", "from", "group", "having", "in", "integer", "is",
    "like", "limit", "max", "min", "not", "null", "offset", "on", "or", "order", "over", "partition",
    "real", "select", "str", "strftime", "sum", "text", "then", "where", "when", "with",
    "data", "result",
}

def _data_query_field_tokens(run: Run) -> set[str]:
    """Best-effort field names a data_query appears to consume or return."""
    tokens = {str(item).strip().lower() for item in (run.returns or []) if str(item).strip()}
    tokens.discard("result")
    # Strip single-quoted string literals first: their contents are VALUES (a LIKE pattern like
    # '%Olivia%', or status = 'Complete'), not column identifiers — tokenizing through them would
    # mis-flag the value text as a missing/unknown column. (Double quotes are SQLite identifiers,
    # left intact so real column refs inside them are still validated.)
    sql = re.sub(r"'[^']*'", " ", run.sql or "")
    ignored = _sql_derived_identifier_tokens(sql)
    for raw in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql):
        token = raw.lower()
        if token in ignored or token in _SQL_NON_FIELD_TOKENS or re.fullmatch(r"table_\d+", token):
            continue
        tokens.add(token)
    return tokens

def _sql_derived_identifier_tokens(sql: str) -> set[str]:
    tokens = {raw.lower() for raw in re.findall(r"\bas\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)}
    tokens.update(_sql_cte_names(sql))
    tokens.update(_sql_table_alias_tokens(sql))
    return tokens

def _sql_cte_names(sql: str) -> set[str]:
    text = sql or ""
    if not re.match(r"^\s*with\b", text, flags=re.I):
        return set()
    return {
        raw.lower()
        for raw in re.findall(r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", text, flags=re.I)
    }

def _sql_table_alias_tokens(sql: str) -> set[str]:
    """Best-effort aliases for derived tables, e.g. `FROM (...) c, (...) comp`."""
    aliases = {
        raw.lower()
        for raw in re.findall(r"\)\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)
    }
    return {alias for alias in aliases if alias not in _SQL_NON_FIELD_TOKENS}

def _sql_referenced_tables(sql: str) -> set[str]:
    return {
        raw.lower()
        for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)
    }

def _missing_query_fields(needed: set[str], available: set[str], ignored: set[str] | None = None) -> set[str]:
    ignored = ignored or set()
    return {
        token for token in needed
        if token not in ignored and not _query_field_available(token, available)
    }

def _query_field_available(token: str, available: set[str]) -> bool:
    if token in available:
        return True
    for suffix in ("_num", "_ts"):
        if token.endswith(suffix) and token[: -len(suffix)] in available:
            return True
    return False

def _read_looks_like_row_collection(run: Run) -> bool:
    """True for read steps that describe per-row collection (old-style; should now be foreach)."""
    if run.kind != "read" or not run.returns:
        return False
    text = f"{run.name}\n{run.read_spec}".lower()
    return any(
        marker in text
        for marker in (
            "逐行", "每行", "每一行", "每条记录", "每条", "行对象", "row object", "one object per row",
        )
    )

def _check_foreach_data_query(stmts: list[Stmt], issues: IssueList) -> None:
    """Guard foreach/data_query sequencing:

    1. Old-style row-collection read → data_query without a foreach is rejected (missing enrichment).
    2. data_query must only reference tables produced by a preceding foreach (not made-up names).
    3. data_query on a foreach into-table must only use fields that foreach body returns produced.
    """

    foreach_tables: dict[str, tuple[str, set[str], bool]] = {}

    def _body_result_fields(seq: list[Stmt], row_fields: set[str]) -> set[str]:
        fields = set(row_fields)
        for item in seq:
            if isinstance(item, Run) and item.returns:
                fields.update(field.lower() for field in item.returns)
            elif isinstance(item, If):
                fields.update(_body_result_fields(item.then, row_fields))
                fields.update(_body_result_fields(item.otherwise, row_fields))
        return fields

    def _referenced_tables(sql: str) -> set[str]:
        return {
            raw.lower()
            for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", sql or "", flags=re.I)
        }

    # Track ALL read vars (list-like or not) for foreach row_fields inference.
    # list-like reads also trigger direct-query guards; plain reads only provide row fields.
    all_read_vars: dict[str, tuple[str, set[str]]] = {}

    def _walk_seq(seq: list[Stmt], pending: dict[str, tuple[str, set[str]]]) -> None:
        local = dict(pending)
        for s in seq:
            if isinstance(s, Run):
                if s.kind == "read" and s.var and s.returns:
                    # Track all read vars for foreach row_fields inference.
                    all_read_vars[s.var] = (s.name, {field.lower() for field in s.returns})
                    if _read_looks_like_row_collection(s):
                        local[s.var] = (s.name, {field.lower() for field in s.returns})
                        continue
                if s.kind == "data_query":
                    needed = _data_query_field_tokens(s)
                    if local and needed:
                        for var, (read_name, row_fields) in local.items():
                            missing = _missing_query_fields(needed, row_fields)
                            if missing:
                                issues.add("FOREACH_DQ_ROW_FIELD_MISSING", 
                                    f"data_query 步「{s.name}」紧跟逐行采集 read「{read_name}」后，"
                                    f"使用/返回了该行未读出的字段 {sorted(missing)}；"
                                    "若这些字段只在每条记录详情里，必须先插入 foreach（name 描述集合，returns 列出每行字段）"
                                    "，body 里写打开详情的 run 并通过 returns/read_spec 产出详情字段，"
                                    "foreach 用 into 产出汇总表；然后 data_query 只能查询该 into 表。"
                                    "不要跳过 foreach 凭空查询详情字段。"
                                )
                                break
                    refs = _referenced_tables(s.sql) - _sql_cte_names(s.sql)
                    unknown_refs = {
                        ref for ref in refs
                        if ref not in foreach_tables and ref != "data" and not re.fullmatch(r"table_\d+", ref)
                    }
                    if local and unknown_refs:
                        issues.add("FOREACH_DQ_UNKNOWN_TABLE", 
                            f"data_query 步「{s.name}」查询了未由前序 foreach 产出的表 {sorted(unknown_refs)}；"
                            "data_query 只能查询 foreach 的 into 表。"
                            "若需要补详情字段，请先写 foreach（name/returns），body 用打开详情的 run + returns/read_spec "
                            "产出 into 表，再查询该表。"
                        )
                    for table in refs & set(foreach_tables):
                        table_label, fields, body_empty = foreach_tables[table]
                        # Exclude data_query returns from the check: they are output aliases
                        # (e.g. "SUM(...) AS total"), not fields that must come from the foreach table.
                        returns_aliases = {str(r).strip().lower() for r in (s.returns or [])}
                        missing = _missing_query_fields(needed, fields, refs | returns_aliases)
                        if missing:
                            if body_empty:
                                issues.add("FOREACH_DQ_GRID_FIELD_MISSING", 
                                    f"data_query 步「{s.name}」查询 foreach body=[] 产出的网格表「{table_label}」，"
                                    f"但 SQL 需要的字段 {sorted(missing)} 没有被该 foreach returns 采集；"
                                    "请把这些字段的基础网格列加入 foreach returns（例如需要 *_ts 就采集对应日期/时间列，"
                                    "需要 *_num 就采集对应金额/数字列），不要因此改成逐条钻取。"
                                )
                            else:
                                issues.add("FOREACH_DQ_DETAIL_FIELD_MISSING", 
                                    f"data_query 步「{s.name}」查询 foreach 产出的表「{table_label}」，"
                                    f"但使用/返回了 foreach body 没有通过 returns 产出的字段 {sorted(missing)}；"
                                    "请在该 foreach body 里逐条打开详情，并让打开详情的 run 带 returns/read_spec 产出这些字段，"
                                    "再对 into 表 data_query。"
                                )
                            break
                    if foreach_tables and not (refs & set(foreach_tables)):
                        produced: set[str] = set()
                        labels: list[str] = []
                        for label, fields, _body_empty in foreach_tables.values():
                            labels.append(label)
                            produced.update(fields)
                        missing = _missing_query_fields(needed, produced, refs)
                        if missing:
                            issues.add("FOREACH_DQ_POST_FOREACH_FIELD_MISSING", 
                                f"data_query 步「{s.name}」位于 foreach 之后，但使用/返回了此前 foreach "
                                f"没有通过 returns 产出的字段 {sorted(missing)}；已存在的 foreach 表为 {labels}。"
                                "若要按每条记录详情字段筛选，请在 foreach body 中用打开详情的 run 返回这些字段，"
                                "并让 SQL 查询对应的 into 表。"
                            )
            elif isinstance(s, ForEach):
                row_fields = set()
                # Use list-like source fields first; fall back to any read var with matching name.
                if s.over in local:
                    row_fields = set(local[s.over][1])
                elif s.over in all_read_vars:
                    row_fields = set(all_read_vars[s.over][1])
                elif s.returns:
                    # new-style foreach: collect_fn provides rows with these fields.
                    # Normalize with _sql_identifier (same as runtime) so "Grand Total (Purchased)"
                    # maps to "grand_total_purchased" and matches what data_query SQL writes.
                    row_fields = {_sql_identifier(r) for r in s.returns}
                table_name = (s.into or f"{s.var}s").lower()
                foreach_tables[table_name] = (
                    s.into or f"{s.var}s",
                    _body_result_fields(s.body, row_fields),
                    not bool(s.body),
                )
                if s.over in local:
                    local.pop(s.over, None)
                _walk_seq(s.body, {})
            elif isinstance(s, If):
                _walk_seq(s.then, dict(local))
                _walk_seq(s.otherwise, dict(local))

    _walk_seq(stmts, {})

def _rank_query_drops_ties(goal_text: str, run: Run) -> bool:
    haystack = f"{goal_text}\n{run.name}\n{run.success_condition}".lower()
    if not re.search(r"\b(second|third|fourth|fifth|rank|most|least)\b|第[二三四五]|最多|最少|排名|并列", haystack):
        return False
    sql = (run.sql or "").lower()
    if not re.search(r"\b(count|group\s+by)\b", sql):
        return False
    return bool(re.search(r"\blimit\s+1\b", sql) or re.search(r"\boffset\s+\d+\b", sql))

_AGGREGATE_FN_RE = re.compile(r"\b(?:sum|avg|min|max|count)\s*\(", flags=re.IGNORECASE)

def _aggregate_query_limits_after_aggregation(sql: str) -> bool:
    text = sql or ""
    words = list(_sql_words_with_depth(text))
    for idx, (select_pos, word, depth) in enumerate(words):
        if word != "select":
            continue
        from_pos = None
        limit_pos = None
        group_pos = None
        for pos, next_word, next_depth in words[idx + 1:]:
            if next_depth < depth:
                break
            if next_depth != depth:
                continue
            if next_word == "select":
                break
            if next_word == "from" and from_pos is None:
                from_pos = pos
            elif next_word == "group" and from_pos is not None and group_pos is None:
                group_pos = pos
            elif next_word == "limit" and from_pos is not None:
                limit_pos = pos
                break
        if from_pos is not None and limit_pos is not None and group_pos is None:
            if _AGGREGATE_FN_RE.search(text[select_pos:from_pos]):
                return True
    return False

def _temporal_limit_without_order(goal_text: str, run: Run) -> bool:
    haystack = f"{goal_text}\n{run.name}\n{run.success_condition}".lower()
    if not re.search(r"\b(last|recent|latest|newest|oldest)\b|最近|最后|最新|最旧|最早", haystack):
        return False
    return _sql_has_limit_without_same_level_order(run.sql or "")

def _temporal_aggregate_without_row_limit(goal_text: str, run: Run) -> bool:
    haystack = f"{goal_text}\n{run.name}\n{run.success_condition}".lower()
    if not re.search(r"\b(last|recent|latest|newest|oldest)\b|最近|最后|最新|最旧|最早", haystack):
        return False
    sql = (run.sql or "").lower()
    if not _AGGREGATE_FN_RE.search(sql):
        return False
    if re.search(r"\blimit\s+\d+\b", sql):
        return False
    if re.search(r"\b(row_number|rank|dense_rank)\s*\(", sql):
        return False
    return True

def _sql_has_limit_without_same_level_order(sql: str) -> bool:
    words = list(_sql_words_with_depth(sql or ""))
    for idx, (select_pos, word, depth) in enumerate(words):
        if word != "select":
            continue
        limit_pos = None
        order_pos = None
        for pos, next_word, next_depth in words[idx + 1:]:
            if next_depth < depth:
                break
            if next_depth != depth:
                continue
            if next_word == "select":
                break
            if next_word == "order" and order_pos is None:
                order_pos = pos
            elif next_word == "limit":
                limit_pos = pos
                break
        if limit_pos is not None and order_pos is None:
            return True
    return False

def _top_level_sql_keyword_pos(sql: str, keyword: str, *, start: int = 0) -> int | None:
    target = keyword.lower()
    for pos, word in _top_level_sql_words(sql):
        if pos >= start and word == target:
            return pos
    return None

def _top_level_sql_words(sql: str):
    for pos, word, depth in _sql_words_with_depth(sql):
        if depth == 0:
            yield pos, word

def _sql_words_with_depth(sql: str):
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote:
            if quote == "]":
                if ch == "]":
                    quote = None
            elif ch == quote:
                if nxt == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "[":
            quote = "]"
            i += 1
            continue
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i + 2)
            i = len(sql) if end == -1 else end + 1
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = len(sql) if end == -1 else end + 2
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            yield i, sql[i:j].lower(), depth
            i = j
            continue
        i += 1

def _sql_uses_schema_mapping_text(sql: str) -> bool:
    """Reject copied schema display forms such as `Header->column` in SQL."""
    return bool(re.search(r"\b[a-zA-Z_][\w .\"'`-]*\s*->\s*[a-zA-Z_]\w*\b", sql or ""))

def _sql_contains_template_ref(sql: str) -> bool:
    """SQL is not a template surface; `{var[field]}` belongs in run text/finish only."""
    return bool(re.search(r"\{[^{}]+\}", sql or ""))

def _sql_uses_quoted_display_identifier(sql: str) -> bool:
    """Reject quoted UI labels such as `"Item Name"`; data_query columns are snake_case."""
    for pattern in (r'"([^"]+)"', r"`([^`]+)`", r"\[([^\]]+)\]"):
        for raw in re.findall(pattern, sql or ""):
            text = str(raw or "").strip()
            if text and _sql_identifier(text) != text:
                return True
    return False

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

def _sql_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text
