"""Program Decomposer: user goal -> DSL Program (the orchestrator's #2).

Replaces the milestone-DAG decompose with a PROGRAM decompose: a goal becomes a small
sequence of milestone-level run() statements plus control flow (if / finish). The LLM
produces a flat, LLM-friendly draft (an explicit `op` per step, a `reasoning` CoT field
up front — rigid schemas suppress reasoning, see structured_read), which we convert to
the clean Program AST deterministically and validate (an if must branch on a real read,
a read must request fields, a finish template must resolve) with one feedback-retry —
the cheap deterministic backstop pattern, not a string-match band-aid.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import (
    feedback_block,
    file_reference_block,
    knowledge_block,
    task_goal_block,
)
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from gui_agent.core.router import IntentResolution, intent_block
from .program import BARE_REF_RE, TEMPLATE_RE, Cond, Finish, ForEach, If, Program, Run, RunKind, Stmt

_SYSTEM = load_prompt_text("task.orchestrator.decomposer")
# Re-decompose reuses the FULL decomposer prompt (DSL grammar + rules 1-10 + examples) and appends a
# "mid-execution revision" framing — the output schema/validation is identical; only the framing
# (re-plan the REMAINING steps from the CURRENT page, absorbing prior experience) differs.
_REDECOMPOSE_SYSTEM = _SYSTEM + "\n\n" + load_prompt_text("task.orchestrator.redecomposer")


class _StepDraft(BaseModel):
    """One DSL step in flat, LLM-friendly form. `op` selects which fields matter."""

    op: str = Field(default="run", description='"run" | "if" | "finish"')
    # --- op=run ---
    var: str = Field(default="", description="把该步结果绑定到的变量名；带 returns/data_query 或后续要引用时填，否则留空")
    name: str = Field(default="", description="op=run：该 milestone 的一句话操作指令")
    success_condition: str = Field(default="", description="op=run：完成后界面应处于的唯一可截图确认终态")
    run_kind: str = Field(default="action", description='op=run：navigation | filter | action | read | data_query')
    precondition: bool = Field(
        default=False,
        description="op=run：该步是否为【前置状态保障】（确保已登录 / 已进入某模式或某页，初始往往已满足）。"
                    "是→true 且 run_kind 用 navigation；普通去某页/做某操作就留 false。",
    )
    returns: list[str] = Field(default_factory=list, description="op=run：该步完成后要返回的结果字段；data_query 也必须填")
    read_spec: str = Field(
        default="",
        description="op=run 且 returns 非空：返回值读取说明——逐个说明每个 returns 字段在完成帧上看什么、"
                    "如何把信号(图标/颜色/文字/位置)判读成值、各取值含义。",
    )
    sql: str = Field(
        default="",
        description="op=run 且 run_kind=data_query：只读 SQL。只能 SELECT/WITH SELECT；默认表名 data，列名为表头 snake_case。",
    )
    data_scope: str = Field(
        default="complete",
        description='op=run 且 run_kind=data_query：complete | current。complete=要求完整数据；current=只分析当前页面/当前可见/当前已渲染行。',
    )
    # --- op=foreach（通用迭代：对某个列表型 read 的每一行跑一遍 body）---
    loop_var: str = Field(default="", description="op=foreach：循环变量名，body 里用 {循环变量[字段]} 引用当前行")
    over: str = Field(default="", description="op=foreach：被迭代的列表来源（旧路径，iPhone/Android 兼容）；browser 新路径留空，由 name/returns 驱动")
    into: str = Field(default="", description="op=foreach：累积表变量名（留空默认 = 循环变量+s）；循环结束后可被 data_query 查询")
    body: list["_StepDraft"] = Field(default_factory=list, description="op=foreach：每行执行一遍的步骤（run/if/finish，不可再嵌 foreach）")
    # --- op=if ---
    cond_var: str = Field(default="", description="op=if：条件依据的变量名（某个带 returns/data_query 步的 var）")
    cond_field: str = Field(default="", description="op=if：读取字段名（该步 returns 里的字段）")
    cond_cmp: str = Field(
        default="==",
        description='op=if：条件操作符："==" | "!=" | "exists" | "empty" | "contains" | "not_contains" | "in" | "not_in"',
    )
    cond_value: str = Field(default="", description="op=if：单个期望值；用于 ==、!=、contains、not_contains")
    cond_values: list[str] = Field(default_factory=list, description="op=if：多个候选值；仅用于 in、not_in")
    then: list["_StepDraft"] = Field(default_factory=list, description="op=if：条件成立时执行的步骤")
    otherwise: list["_StepDraft"] = Field(default_factory=list, description="op=if：条件不成立时执行的步骤")
    # --- op=finish ---
    message: str = Field(default="", description="op=finish：最终答复模板，可用 {变量[字段]} 引用某步返回值")


class _PlanDraft(BaseModel):
    reasoning: str = Field(
        default="",
        description="先分析任务：要到哪些页、做什么操作、读什么结果、是否需要条件分支；再据此写 steps",
    )
    goal: str = Field(default="", description="任务一句话描述")
    steps: list[_StepDraft] = Field(default_factory=list)


_StepDraft.model_rebuild()

_VALID_KINDS = {"navigation", "filter", "action", "read", "data_query"}
_VALID_CMPS = {"==", "!=", "exists", "empty", "contains", "not_contains", "in", "not_in"}
_CMP_ALIASES = {
    "=": "==",
    "eq": "==",
    "equals": "==",
    "ne": "!=",
    "neq": "!=",
    "not_equals": "!=",
    "not equals": "!=",
    "not-empty": "exists",
    "not_empty": "exists",
    "not empty": "exists",
    "is_not_empty": "exists",
    "is not empty": "exists",
    "is-empty": "empty",
    "is_empty": "empty",
    "is empty": "empty",
    "not-contains": "not_contains",
    "not contains": "not_contains",
    "not-in": "not_in",
    "not in": "not_in",
}


def _to_kind(raw: str) -> RunKind:
    k = (raw or "").strip().lower()
    return k if k in _VALID_KINDS else "action"  # type: ignore[return-value]


def _to_cmp(raw: str):
    k = (raw or "").strip().lower()
    k = _CMP_ALIASES.get(k, k)
    return k if k in _VALID_CMPS else "=="  # type: ignore[return-value]


def _split_cond_values(raw: str) -> list[str]:
    text = raw or ""
    for sep in ("|", "、", "，", ";", "；", "\n"):
        text = text.replace(sep, ",")
    return [p.strip() for p in text.split(",") if p.strip()]


def _to_cond_values(cmp: str, values: list[str], value: str) -> list[str]:
    out = [v.strip() for v in values if v.strip()]
    if not out and cmp in {"in", "not_in"} and value.strip():
        out = _split_cond_values(value)
    return out


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


def _to_stmts(drafts: list[_StepDraft]) -> list[Stmt]:
    """Deterministically convert flat step drafts into the clean Program AST."""
    out: list[Stmt] = []
    for d in drafts:
        op = (d.op or "run").strip().lower()
        if op == "finish":
            out.append(Finish(message=d.message))
        elif op == "foreach":
            # One level only: drop any nested foreach in the body (decomposer prompt forbids it; this
            # is the deterministic backstop) so a malformed nested loop can't reach the interpreter.
            body = [s for s in _to_stmts(d.body) if not isinstance(s, ForEach)]
            out.append(ForEach(
                var=(d.loop_var or "item").strip(),
                over=d.over.strip(),
                target=d.name.strip(),
                returns=[r for r in d.returns if r.strip()],
                into=d.into.strip(),
                body=body,
            ))
        elif op == "if":
            cmp = _to_cmp(d.cond_cmp)
            out.append(
                If(
                    cond=Cond(
                        var=d.cond_var,
                        field=d.cond_field,
                        cmp=cmp,
                        value=d.cond_value,
                        values=_to_cond_values(cmp, d.cond_values, d.cond_value),
                    ),
                    then=_to_stmts(d.then),
                    otherwise=_to_stmts(d.otherwise),
                )
            )
        else:  # run (default)
            out.append(
                Run(
                    var=(d.var.strip() or None),
                    name=d.name,
                    success_condition=d.success_condition,
                    kind=_to_kind(d.run_kind),
                    returns=[r for r in d.returns if r.strip()],
                    read_spec=d.read_spec,
                    sql=d.sql,
                    data_scope="current" if (d.data_scope or "").strip().lower() == "current" else "complete",
                    precondition=bool(d.precondition),
                )
            )
    return out


def to_program(draft: _PlanDraft, goal: str) -> Program:
    return Program(goal=draft.goal or goal, statements=_to_stmts(draft.steps))


def validate_program(program: Program) -> list[str]:
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
    issues: list[str] = []
    if not program.statements:
        return ["程序为空：至少要有一个 run 步骤"]
    if _goal_expects_structured_answer(program.goal) and not _has_result_source(program.statements):
        issues.append(
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
                issues.append(
                    f"{where} 引用的 {{{var}[{field}]}} 中变量「{var}」在此处尚未产生"
                    f"（不是任何在它之前、且在当前执行路径上的返回值/data_query 步的 var；引用在前/读取在后/读取在另一分支都算）"
                    f"——指代落空；请先让前序步骤通过 returns 或 data_query 产生「{var}」并放在引用它之前（同一执行路径上），或删掉这个引用"
                )
            elif field not in scope[var]:
                issues.append(
                    f"{where} 引用的字段「{var}[{field}]」不在该步骤返回的字段（returns）里"
                    f"——请改用它 returns 里已有的字段名，或在该步 returns 里补上「{field}」"
                )
        # botched bare {var}: a known read var written without [field] — neither resolves nor
        # matches the template, so the literal "{var}" leaks to the planner (回归 20260615_194320:
        # 「编辑机器人 {robot_name}」漏给了 planner). Force the {var[field]} form via repair.
        for m in BARE_REF_RE.finditer(text or ""):
            var = m.group(1)
            if var in all_result_vars:
                issues.append(
                    f"{where} 用了裸 {{{var}}} 缺字段——「{var}」是某个返回值/data_query 步的结果变量，"
                    f"引用它的某个字段要写成 {{{var}[字段]}}（字段取自该步 returns）；裸 {{{var}}} 不会被填上值"
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
                    issues.append(
                        f"步骤「{s.name}」标了 precondition=true 但 run_kind={s.kind}——"
                        "前置状态保障只能是 navigation 步（确保已到达/已进入某状态），"
                        "不能标在 action/read/filter/data_query 上；请改成 navigation，或去掉 precondition"
                    )
                if s.kind == "read" and not s.returns:
                    issues.append(f"read 步「{s.name}」没有 returns 字段——read 必须指定要读取的字段")
                if s.kind == "read" and not s.var:
                    issues.append(f"read 步「{s.name}」没有绑定 var——读到的结果无法被后续引用")
                if s.kind == "data_query" and not s.returns:
                    issues.append(f"data_query 步「{s.name}」没有 returns 字段——data_query 必须指定要返回的字段")
                if s.kind == "data_query" and not s.var:
                    issues.append(f"data_query 步「{s.name}」没有绑定 var——查询结果无法被后续引用")
                if s.kind == "data_query" and not s.sql.strip():
                    issues.append(f"data_query 步「{s.name}」没有 sql——必须提供只读 SELECT/WITH SELECT")
                if s.returns and s.kind != "data_query" and not s.var:
                    issues.append(f"步骤「{s.name}」声明了 returns 但没有绑定 var——返回值无法被后续 if/finish/foreach 引用")
                if s.returns and s.kind not in {"read", "data_query"} and not s.read_spec.strip():
                    issues.append(f"步骤「{s.name}」声明了 returns 但没有 read_spec——必须说明这些返回字段在完成帧上如何读取")
                if s.kind == "data_query" and _sql_uses_schema_mapping_text(s.sql):
                    issues.append(
                        f"data_query 步「{s.name}」的 SQL 使用了 schema 显示映射文本（如 Header->column）。"
                        "SQL 只能写实际 normalized column identifiers，例如写 column，不要写 Header->column。"
                    )
                if s.kind == "data_query" and _sql_uses_quoted_display_identifier(s.sql):
                    issues.append(
                        f"data_query 步「{s.name}」的 SQL 使用了带空格/标点的 quoted display label。"
                        "data_query 表格列已经归一化为 snake_case 标识符；SQL 应写 normalized column identifiers，"
                        "不要写 UI 表头/标签的双引号形式。"
                    )
                if s.kind == "data_query" and _rank_query_drops_ties(rank_goal_text, s):
                    issues.append(
                        f"data_query 步「{s.name}」像是在回答聚合排名/第N多，但 SQL 使用 LIMIT/OFFSET 单行截断，"
                        "会丢掉并列项；请先 GROUP BY 计算 count，再用 DENSE_RANK() 或 HAVING count 返回该名次的所有并列结果"
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
                    issues.append(
                        f"if 条件引用的变量「{s.cond.var}」在此处尚未产生"
                        "（不是任何在它之前、且在当前执行路径上的返回值/data_query 步的 var）"
                        f"——请在这个 if 之前（同一执行路径上）让某个步骤通过 returns 或 data_query 产生「{s.cond.var}」"
                    )
                elif s.cond.field not in scope[s.cond.var]:
                    issues.append(
                        f"if 条件字段「{s.cond.var}[{s.cond.field}]」不在该步骤返回的字段（returns）里"
                        f"——请把 cond_field 改成该步 returns 里已有的字段名，或在该步 returns 里补上「{s.cond.field}」"
                    )
                if s.cond.cmp in {"contains", "not_contains"} and not s.cond.value.strip():
                    issues.append(
                        f"if 条件「{s.cond.var}[{s.cond.field}] {s.cond.cmp}」缺少 cond_value——"
                        "contains/not_contains 必须给出要匹配的文字"
                    )
                if s.cond.cmp in {"in", "not_in"} and not [v for v in s.cond.values if v.strip()]:
                    issues.append(
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
                    issues.append(
                        f"foreach 的 over「{s.over}」在此处尚未产生"
                        "（不是任何在它之前、且在当前执行路径上的 read 步的 var）；"
                        f"请在这个 foreach 之前让某个 read 步产生「{s.over}」"
                    )
                if not s.var:
                    issues.append("foreach 缺少循环变量名（loop_var）——body 需要用 {循环变量[字段]} 引用当前行")
                if not s.body and not s.returns:
                    issues.append(f"foreach（循环变量「{s.var}」）的 body 为空且未设置 returns——"
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


def _check_retrieval_retry_preserves_field(stmts: list[Stmt], issues: list[str]) -> None:
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
                                issues.append(
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
    "all", "and", "as", "asc", "between", "by", "case", "cast", "count", "dense_rank",
    "desc", "distinct", "else", "end", "from", "group", "having", "in", "integer", "is",
    "like", "limit", "not", "null", "offset", "on", "or", "order", "over", "partition",
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
    for raw in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", sql):
        token = raw.lower()
        if token in _SQL_NON_FIELD_TOKENS or re.fullmatch(r"table_\d+", token):
            continue
        tokens.add(token)
    return tokens


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


def _check_foreach_data_query(stmts: list[Stmt], issues: list[str]) -> None:
    """Guard foreach/data_query sequencing:

    1. Old-style row-collection read → data_query without a foreach is rejected (missing enrichment).
    2. data_query must only reference tables produced by a preceding foreach (not made-up names).
    3. data_query on a foreach into-table must only use fields that foreach body returns produced.
    """

    foreach_tables: dict[str, tuple[str, set[str]]] = {}

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
                            missing = needed - row_fields
                            if missing:
                                issues.append(
                                    f"data_query 步「{s.name}」紧跟逐行采集 read「{read_name}」后，"
                                    f"使用/返回了该行未读出的字段 {sorted(missing)}；"
                                    "若这些字段只在每条记录详情里，必须先插入 foreach（name 描述集合，returns 列出每行字段）"
                                    "，body 里写打开详情的 run 并通过 returns/read_spec 产出详情字段，"
                                    "foreach 用 into 产出汇总表；然后 data_query 只能查询该 into 表。"
                                    "不要跳过 foreach 凭空查询详情字段。"
                                )
                                break
                    refs = _referenced_tables(s.sql)
                    unknown_refs = {
                        ref for ref in refs
                        if ref not in foreach_tables and ref != "data" and not re.fullmatch(r"table_\d+", ref)
                    }
                    if local and unknown_refs:
                        issues.append(
                            f"data_query 步「{s.name}」查询了未由前序 foreach 产出的表 {sorted(unknown_refs)}；"
                            "data_query 只能查询 foreach 的 into 表。"
                            "若需要补详情字段，请先写 foreach（name/returns），body 用打开详情的 run + returns/read_spec "
                            "产出 into 表，再查询该表。"
                        )
                    for table in refs & set(foreach_tables):
                        table_label, fields = foreach_tables[table]
                        missing = needed - fields - refs
                        if missing:
                            issues.append(
                                f"data_query 步「{s.name}」查询 foreach 产出的表「{table_label}」，"
                                f"但使用/返回了 foreach body 没有通过 returns 产出的字段 {sorted(missing)}；"
                                "请在该 foreach body 里逐条打开详情，并让打开详情的 run 带 returns/read_spec 产出这些字段，"
                                "再对 into 表 data_query。"
                            )
                            break
                    if foreach_tables and not (refs & set(foreach_tables)):
                        produced: set[str] = set()
                        labels: list[str] = []
                        for label, fields in foreach_tables.values():
                            labels.append(label)
                            produced.update(fields)
                        missing = needed - produced - refs
                        if missing:
                            issues.append(
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
                    # new-style foreach: collect_fn provides rows with these fields
                    row_fields = {r.lower() for r in s.returns}
                table_name = (s.into or f"{s.var}s").lower()
                foreach_tables[table_name] = (s.into or f"{s.var}s", _body_result_fields(s.body, row_fields))
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


def _sql_uses_schema_mapping_text(sql: str) -> bool:
    """Reject copied schema display forms such as `Header->column` in SQL."""
    return bool(re.search(r"\b[a-zA-Z_][\w .\"'`-]*\s*->\s*[a-zA-Z_]\w*\b", sql or ""))


def _sql_uses_quoted_display_identifier(sql: str) -> bool:
    """Reject quoted UI labels such as `"Item Name"`; data_query columns are snake_case."""
    for pattern in (r'"([^"]+)"', r"`([^`]+)`", r"\[([^\]]+)\]"):
        for raw in re.findall(pattern, sql or ""):
            text = str(raw or "").strip()
            if text and _sql_identifier(text) != text:
                return True
    return False


_MAX_RETRIES = 2


def _corrective_directive_block(corrective_directive: str) -> "ContextBlock | None":
    """Feasibility Guard kick-back: a runtime correction from the supervisor (the milestone was
    judged infeasible). It must NOT be buried in app_navigation knowledge — it's an authoritative
    constraint, so it gets its own block, priority ABOVE the goal (15 < 20) and `required` budget
    so the budgeter never drops it. Shared by decompose() and redecompose()."""
    if not corrective_directive.strip():
        return None
    return ContextBlock(
        id="runtime.corrective_directive",
        budget="required",
        source_type="runtime_state",
        source="feasibility_kickback",
        ttl="task",
        priority=15,
        content=(
            "## ⚠️ 上层纠正指令\n"
            "【来源：上层运行时纠正（基于真实界面观察）｜权威级别：最高｜必须服从】\n"
            + corrective_directive.strip()
            + "\n\n依据上下文优先级裁决规则，本指令高于应用知识与默认习惯：与它们冲突时一律以本指令为准。"
        ),
    )


def _page_and_table_blocks(
    current_url: str, current_site: str, current_title: str, table_summaries: list[dict] | None
) -> list["ContextBlock"]:
    """Ground-truth front-tab identity + current table inventory. Shared by decompose()/redecompose()
    so the re-decompose sees the CURRENT page (not a frozen start-of-run frame)."""
    blocks: list[ContextBlock] = []
    if current_url or current_site:
        # The screenshot omits the omnibox. Lead with a semantic site name + page title; the raw
        # url (often an IP) is a tail. Lets the planner skip a navigation milestone when already on
        # the target page.
        _parts = []
        if current_site:
            _parts.append(f"站点：{current_site}（已知应用）")
        if current_title:
            _parts.append(f"页面：{current_title}")
        if current_url:
            _parts.append(f"url：{current_url}")
        page = "\n## 当前前台页面（以此为准，截图看不到地址栏）：" + " · ".join(_parts)
        page += (
            "\n若当前已在任务目标站点，可省略『打开该站点』这类重复 milestone；"
            "但不要省略必要的页内定位/切换 tab/打开目标页面，也不要省略清除或设置筛选、搜索、排序等会改变数据源口径的 UI 步骤。"
            "视觉 read 前必须先让目标区域处于当前可见终态；"
            "若目标表格已出现在『当前结构化表格』列表中，只有当它已经处于任务要求的筛选/排序/范围终态时，data_query 才可直接查询该表格；否则先规划 UI 步骤准备数据源。"
        )
        blocks.append(ContextBlock(
            id="runtime.observation.browser_page",
            budget="high",
            source_type="runtime_state",
            source="observation",
            ttl="turn",
            priority=30,
            content=page,
        ))
    table_hint = _table_schema_prompt(table_summaries)
    if table_hint:
        blocks.append(ContextBlock(
            id="runtime.observation.table_schema",
            budget="high",
            source_type="runtime_state",
            source="browser_tables",
            ttl="turn",
            priority=35,
            content=table_hint,
        ))
    return blocks


def _invoke_plan(
    *,
    system_prompt: str,
    png_bytes: bytes | None,
    context_blocks: list["ContextBlock | None"],
    goal: str,
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None,
    context_reports: list[dict] | None,
    label: str,
) -> Program:
    """Shared LLM call + deterministic validate/feedback-retry. Both decompose() and redecompose()
    assemble their own context blocks, then hand off here for the identical draft→AST→validate loop."""
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    issues: list[str] = []
    program = Program(goal=goal, statements=[])
    for attempt in range(_MAX_RETRIES + 1):
        messages = assemble_messages(
            system_prompt,
            png_bytes,
            human_blocks=[*context_blocks, feedback_block(issues)],
            image_resize="none",
            prepare_vision_prompt_png=prepare_vision_prompt_png,
            label=label,
            context_reports=context_reports,
            decision_text="",
        )
        draft = invoke_structured(llm, messages, _PlanDraft, trace_sink=context_reports, trace_label=label)
        program = to_program(draft, goal)
        issues = validate_program(program)
        if not issues:
            break
        if attempt < _MAX_RETRIES:
            print(f"  [Orchestrator] 程序分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{_MAX_RETRIES})...")
            for i in issues:
                print(f"  [Orchestrator]   {i}")
    return program


def _iter_runs(stmts: list[Stmt]):
    for stmt in stmts:
        if isinstance(stmt, Run):
            yield stmt
        elif isinstance(stmt, If):
            yield from _iter_runs(stmt.then)
            yield from _iter_runs(stmt.otherwise)
        elif isinstance(stmt, ForEach):
            yield from _iter_runs(stmt.body)


def _normalize_approximate_entity_sql(
    program: Program,
    resolution: "IntentResolution | None",
) -> Program:
    """Use intent search keys, not approximate spoken mentions, inside SQL filters."""
    if resolution is None or not resolution.entities:
        return program
    replacements: list[tuple[str, str]] = []
    for entity in resolution.entities:
        if entity.match_mode != "approximate":
            continue
        mention = (entity.mention or "").strip()
        key = (entity.search_key or "").strip()
        if not mention or not key or _norm_sql_text(mention) == _norm_sql_text(key):
            continue
        replacements.append((mention, key.replace("'", "''")))
    if not replacements:
        return program
    updated = program.model_copy(deep=True)
    for run in _iter_runs(updated.statements):
        if run.kind != "data_query" or not run.sql:
            continue
        sql = run.sql
        for mention, key in replacements:
            sql = re.sub(re.escape(mention), key, sql, flags=re.IGNORECASE)
        run.sql = sql
    return updated


def _norm_sql_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def decompose(
    goal: str,
    *,
    png_bytes: bytes | None = None,
    knowledge: str = "",
    file_section: str = "",
    system_prompt: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
    table_summaries: list[dict] | None = None,
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
    corrective_directive: str = "",
    resolution: "IntentResolution | None" = None,
) -> Program:
    """Decompose a user goal into a DSL Program via LLM + deterministic validate/retry.

    `png_bytes` (current screen) gives the planner page context; `knowledge` injects app
    navigation knowledge; `file_section` is the resolved content of any `@<path>` refs in the
    goal (config field values the spoken goal only points at — see resolve_file_refs);
    `system_prompt` overrides the default DSL prompt (platform tuning); `resolution` is the
    router's upfront entity classification (fuzzy-allowed? which key?) — rendered as a FACTS-ONLY
    context block right after the goal (see intent_block); decompose owns translating it into the
    retrieval ladder (rule 4b), not the fuzzy/exact decision itself.
    `prepare_vision_prompt_png` is the platform bundle's vision prompt image hook:
    iPhone downscales Retina frames, browser/android keep native observations.
    """
    context_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        intent_block(resolution),
        _corrective_directive_block(corrective_directive),
        file_reference_block(file_section),
        knowledge_block("app_navigation", knowledge),
        *_page_and_table_blocks(current_url, current_site, current_title, table_summaries),
    ]
    program = _invoke_plan(
        system_prompt=system_prompt or _SYSTEM,
        png_bytes=png_bytes,
        context_blocks=context_blocks,
        goal=goal,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
        context_reports=context_reports,
        label="orchestrator.decompose",
    )
    return _normalize_approximate_entity_sql(program, resolution)


def _prior_experience_block(prior_experience: str) -> "ContextBlock | None":
    """Executed steps + their outcomes — the run's accumulated EXPERIENCE (which routes already
    failed, what data was already read). Context, NOT a to-do: don't redo these. Priority 16, just
    under the corrective directive (15)."""
    if not prior_experience.strip():
        return None
    return ContextBlock(
        id="runtime.prior_experience",
        budget="high",
        source_type="runtime_state",
        source="redecompose_progress",
        ttl="task",
        priority=16,
        content=(
            "## 已执行步骤与结果（经验，勿重做）\n"
            "【这是本次执行已经跑完的部分及其结果——是你的经验/上下文，不是要重做的清单。"
            "默认这些步骤的终态已达成；据此避开已被证伪的路径。】\n"
            + prior_experience.strip()
        ),
    )


def _remaining_plan_block(remaining_plan: str) -> "ContextBlock | None":
    """The unexecuted steps — the re-decompose TARGET. Priority 17 (under directive + experience):
    these are what to re-plan, from the current page, per the directive."""
    if not remaining_plan.strip():
        return None
    return ContextBlock(
        id="runtime.remaining_plan",
        budget="required",
        source_type="runtime_state",
        source="redecompose_progress",
        ttl="task",
        priority=17,
        content=(
            "## 剩余计划（重排目标）\n"
            "【以下是原计划里还没执行、需要你重新规划的部分。你的输出 steps 只覆盖这些剩余工作——"
            "从当前真实页面继续、服从上层纠正指令、吸收上面的经验把它们重新展开成可执行步骤。】\n"
            + remaining_plan.strip()
        ),
    )


def redecompose(
    goal: str,
    *,
    remaining_plan: str = "",
    prior_experience: str = "",
    corrective_directive: str = "",
    png_bytes: bytes | None = None,
    knowledge: str = "",
    file_section: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
    table_summaries: list[dict] | None = None,
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
    resolution: "IntentResolution | None" = None,
) -> Program:
    """Re-decompose the REMAINING (unexecuted) plan mid-run — NOT a fresh full-goal decompose.

    Unlike `decompose` (goal → full plan from the start screen), this is invoked after a Feasibility
    kick-back: some milestones already ran (their outcomes are `prior_experience`), one hit a
    correction (`corrective_directive`), and the rest (`remaining_plan`) must be re-planned from the
    CURRENT page (current_url/title/png/table_summaries reflect where the run actually is now, not
    its start). Reuses the full DSL prompt + schema + validation; only the framing differs (see
    redecomposer.md). The returned Program covers only the remaining work.
    """
    context_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        intent_block(resolution),
        _corrective_directive_block(corrective_directive),
        _prior_experience_block(prior_experience),
        _remaining_plan_block(remaining_plan),
        file_reference_block(file_section),
        knowledge_block("app_navigation", knowledge),
        *_page_and_table_blocks(current_url, current_site, current_title, table_summaries),
    ]
    program = _invoke_plan(
        system_prompt=_REDECOMPOSE_SYSTEM,
        png_bytes=png_bytes,
        context_blocks=context_blocks,
        goal=goal,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
        context_reports=context_reports,
        label="orchestrator.redecompose",
    )
    return _normalize_approximate_entity_sql(program, resolution)


def _table_schema_prompt(tables: list[dict] | None) -> str:
    """Compact table inventory for planning SQL, without row/cell values."""
    if not tables:
        return ""
    lines: list[str] = []
    used_aliases: set[str] = set()
    for idx, table in enumerate(tables[:12], start=1):
        if not isinstance(table, dict):
            continue
        headers = table.get("headers") if isinstance(table.get("headers"), list) else []
        aliases = [f"table_{idx}"]
        used_aliases.add(f"table_{idx}")
        if idx == 1:
            aliases.append("data")
            used_aliases.add("data")
        caption = str(table.get("caption") or "").strip()
        caption_alias = _sql_identifier(caption)
        if caption_alias and caption_alias not in used_aliases:
            aliases.append(caption_alias)
            used_aliases.add(caption_alias)
        sql_columns = []
        labels = []
        for header in headers[:24]:
            label = str(header or "").strip()
            column = _sql_identifier(label)
            if not column:
                continue
            sql_columns.append(column)
            if label and label != column:
                labels.append(f'{column} from "{label}"')
        row_count = table.get("row_count")
        try:
            row_text = str(int(str(row_count).replace(",", ""))) if row_count is not None else "?"
        except ValueError:
            row_text = "?"
        completeness = "partial" if table.get("partial") else "complete"
        caption_text = f' caption="{caption}";' if caption else ""
        column_text = ", ".join(sql_columns) if sql_columns else "(no headers)"
        labels_text = f"; source labels: {', '.join(labels)}" if labels else ""
        lines.append(
            f"- {'/'.join(aliases)};{caption_text} sql columns: {column_text}{labels_text}; rows: {row_text}; {completeness}"
        )
    if not lines:
        return ""
    return (
        "\n## 当前结构化表格（仅 schema，不含行数据）\n"
        "这些表格来自当前界面已采集的表格快照；用于规划 data_query 的表名和列名。"
        "这里故意不提供行数据，实际查询由受限 SQLite primitive 在运行时读取。\n"
        + "\n".join(lines)
        + "\n若这些表格已经是任务要求的数据源终态，可生成 data_query；否则先规划导航、筛选/搜索/排序、清除旧筛选或完整采集步骤。SQL 只能使用表名和 sql columns 中列出的 snake_case 标识符。source labels 只是人类可读说明，不是 SQL 语法。"
    )


def _sql_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text
