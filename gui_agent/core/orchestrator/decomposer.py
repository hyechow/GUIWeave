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

from .program import BARE_REF_RE, TEMPLATE_RE, Cond, Finish, If, Program, Run, RunKind, Stmt

_SYSTEM = load_prompt_text("task.orchestrator.decomposer")


class _StepDraft(BaseModel):
    """One DSL step in flat, LLM-friendly form. `op` selects which fields matter."""

    op: str = Field(default="run", description='"run" | "if" | "finish"')
    # --- op=run ---
    var: str = Field(default="", description="把该步结果绑定到的变量名；仅 read/data_query 或后续要引用时填，否则留空")
    name: str = Field(default="", description="op=run：该 milestone 的一句话操作指令")
    success_condition: str = Field(default="", description="op=run：完成后界面应处于的唯一可截图确认终态")
    run_kind: str = Field(default="action", description='op=run：navigation | filter | action | read | data_query')
    precondition: bool = Field(
        default=False,
        description="op=run：该步是否为【前置状态保障】（确保已登录 / 已进入某模式或某页，初始往往已满足）。"
                    "是→true 且 run_kind 用 navigation；普通去某页/做某操作就留 false。",
    )
    returns: list[str] = Field(default_factory=list, description="op=run 且 run_kind=read/data_query：要返回的结果字段名列表")
    read_spec: str = Field(
        default="",
        description="op=run 且 run_kind=read：本次读取说明——逐个说明每个 returns 字段在界面上看什么、"
                    "如何把信号(图标/颜色/文字/位置)判读成值、各取值含义；让纯只读能据此判读。",
    )
    sql: str = Field(
        default="",
        description="op=run 且 run_kind=data_query：只读 SQL。只能 SELECT/WITH SELECT；默认表名 data，列名为表头 snake_case。",
    )
    data_scope: str = Field(
        default="complete",
        description='op=run 且 run_kind=data_query：complete | current。complete=要求完整数据；current=只分析当前页面/当前可见/当前已渲染行。',
    )
    # --- op=if ---
    cond_var: str = Field(default="", description="op=if：条件依据的变量名（某个 read/data_query 步的 var）")
    cond_field: str = Field(default="", description="op=if：读取字段名（该 read/data_query 步 returns 里的字段）")
    cond_cmp: str = Field(
        default="==",
        description='op=if：条件操作符："==" | "!=" | "exists" | "empty" | "contains" | "not_contains" | "in" | "not_in"',
    )
    cond_value: str = Field(default="", description="op=if：单个期望值；用于 ==、!=、contains、not_contains")
    cond_values: list[str] = Field(default_factory=list, description="op=if：多个候选值；仅用于 in、not_in")
    then: list["_StepDraft"] = Field(default_factory=list, description="op=if：条件成立时执行的步骤")
    otherwise: list["_StepDraft"] = Field(default_factory=list, description="op=if：条件不成立时执行的步骤")
    # --- op=finish ---
    message: str = Field(default="", description="op=finish：最终答复模板，可用 {变量[字段]} 引用某 read 结果")


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


def _to_stmts(drafts: list[_StepDraft]) -> list[Stmt]:
    """Deterministically convert flat step drafts into the clean Program AST."""
    out: list[Stmt] = []
    for d in drafts:
        op = (d.op or "run").strip().lower()
        if op == "finish":
            out.append(Finish(message=d.message))
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

    A read/data_query must request fields + bind a var; an if must branch on a field a prior non-UI result returns; a
    precondition may only sit on a navigation step (it ensures a state, so on action/read/filter
    it would be wrongly accepted on frame 1); and every {var[field]} template ref (finish message
    OR — read-then-reference, rule 10 — a run's name/success_condition/read_spec) must resolve to a
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

    all_result_vars: set[str] = set()  # every non-UI result var anywhere — to spot botched bare refs
    rank_goal_text = (program.goal or "").lower()

    def _collect_result_vars(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, Run) and s.kind in {"read", "data_query"} and s.var:
                all_result_vars.add(s.var)
            elif isinstance(s, If):
                _collect_result_vars(s.then)
                _collect_result_vars(s.otherwise)
    _collect_result_vars(program.statements)

    def _check_refs(text: str, where: str, scope: dict[str, set[str]]) -> None:
        # `scope` = read var -> returns, for reads already executed BEFORE this point on this path.
        for m in TEMPLATE_RE.finditer(text or ""):
            var, field = m.group(1), m.group(2).strip().strip("'\"")
            if var not in scope:
                issues.append(
                    f"{where} 引用的 {{{var}[{field}]}} 中变量「{var}」在此处尚未产生"
                    f"（不是任何在它之前、且在当前执行路径上的 read/data_query 步的 var；引用在前/读取在后/读取在另一分支都算）"
                    f"——指代落空；请先加一个 read 或 data_query 步产生「{var}」并放在引用它之前（同一执行路径上），或删掉这个引用"
                )
            elif field not in scope[var]:
                issues.append(
                    f"{where} 引用的字段「{var}[{field}]」不在该 read/data_query 步返回的字段（returns）里"
                    f"——请改用它 returns 里已有的字段名，或在该步 returns 里补上「{field}」"
                )
        # botched bare {var}: a known read var written without [field] — neither resolves nor
        # matches the template, so the literal "{var}" leaks to the planner (回归 20260615_194320:
        # 「编辑机器人 {robot_name}」漏给了 planner). Force the {var[field]} form via repair.
        for m in BARE_REF_RE.finditer(text or ""):
            var = m.group(1)
            if var in all_result_vars:
                issues.append(
                    f"{where} 用了裸 {{{var}}} 缺字段——「{var}」是某个 read/data_query 步的结果变量，"
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
                if s.kind in {"read", "data_query"} and s.var:
                    scope[s.var] = set(s.returns)
            elif isinstance(s, Finish):
                _check_refs(s.message, "finish 模板", scope)
            elif isinstance(s, If):
                if s.cond.var not in scope:
                    issues.append(
                        f"if 条件引用的变量「{s.cond.var}」在此处尚未产生"
                        "（不是任何在它之前、且在当前执行路径上的 read/data_query 步的 var）"
                        f"——请在这个 if 之前（同一执行路径上）加一个 read 或 data_query 步产生「{s.cond.var}」"
                    )
                elif s.cond.field not in scope[s.cond.var]:
                    issues.append(
                        f"if 条件字段「{s.cond.var}[{s.cond.field}]」不在该 read/data_query 步返回的字段（returns）里"
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

    _walk(program.statements, {})
    return issues


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
) -> Program:
    """Decompose a user goal into a DSL Program via LLM + deterministic validate/retry.

    `png_bytes` (current screen) gives the planner page context; `knowledge` injects app
    navigation knowledge; `file_section` is the resolved content of any `@<path>` refs in the
    goal (config field values the spoken goal only points at — see resolve_file_refs);
    `system_prompt` overrides the default DSL prompt (platform tuning).
    `prepare_vision_prompt_png` is the platform bundle's vision prompt image hook:
    iPhone downscales Retina frames, browser/android keep native observations.
    """
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )

    context_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        # mechanism-2 kick-back: a runtime correction from the supervisor (the milestone was judged
        # infeasible). It must NOT be buried in app_navigation knowledge — it's an authoritative
        # constraint, so it gets its own block, priority ABOVE the goal (15 < 20) and `required`
        # budget so the budgeter never drops it. The app knowledge stays for navigation grounding.
        ContextBlock(
            id="runtime.corrective_directive",
            budget="required",
            source_type="runtime_state",
            source="feasibility_kickback",
            ttl="task",
            priority=15,
            content=(
                "## ⚠️ 上层纠正指令（重规划必读，必须严格遵守）\n" + corrective_directive.strip()
                + "\n\n与下方任何应用知识或默认规划习惯冲突时，一律以本纠正指令为准。"
            ),
        ) if corrective_directive.strip() else None,
        file_reference_block(file_section),
        knowledge_block("app_navigation", knowledge),
    ]
    if current_url or current_site:
        # Ground-truth front-tab identity (the screenshot omits the omnibox). Lead with a
        # semantic site name + page title; the raw url (often an IP) is a tail. Lets the
        # decomposer skip a navigation milestone when already on the target site.
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
        context_blocks.append(ContextBlock(
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
        context_blocks.append(ContextBlock(
            id="runtime.observation.table_schema",
            budget="high",
            source_type="runtime_state",
            source="browser_tables",
            ttl="turn",
            priority=35,
            content=table_hint,
        ))
    issues: list[str] = []
    program = Program(goal=goal, statements=[])
    for attempt in range(_MAX_RETRIES + 1):
        messages = assemble_messages(
            system_prompt or _SYSTEM,
            png_bytes,
            human_blocks=[*context_blocks, feedback_block(issues)],
            image_resize="none",
            prepare_vision_prompt_png=prepare_vision_prompt_png,
            label="orchestrator.decompose",
            context_reports=context_reports,
            decision_text="",
        )
        draft = invoke_structured(
            llm,
            messages,
            _PlanDraft,
            trace_sink=context_reports,
            trace_label="orchestrator.decompose",
        )
        program = to_program(draft, goal)
        issues = validate_program(program)
        if not issues:
            break
        if attempt < _MAX_RETRIES:
            print(f"  [Orchestrator] 程序分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{_MAX_RETRIES})...")
            for i in issues:
                print(f"  [Orchestrator]   {i}")
    return program


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
