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
from pydantic import BaseModel, Field, field_validator

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
from .program import (
    Call,
    Compute,
    Cond,
    Finish,
    ForEach,
    FunctionDef,
    If,
    Program,
    Query,
    Read,
    Run,
    RunKind,
    RunLike,
    Stmt,
)
from .intent_contracts import IntentContractIssue, validate_intent_contracts
from .data_query import _rewrite_quoted_display_identifiers
from .validator import (  # validator lives in its own module; decompose imports it back
    ValidationIssue,
    validate_program,
    _sql_identifier,
)

_SYSTEM = load_prompt_text("task.orchestrator.decomposer")
# Re-decompose reuses the FULL decomposer prompt (DSL grammar + rules 1-10 + examples) and appends a
# "mid-execution revision" framing — the output schema/validation is identical; only the framing
# (re-plan the REMAINING steps from the CURRENT page, absorbing prior experience) differs.
_REDECOMPOSE_SYSTEM = _SYSTEM + "\n\n" + load_prompt_text("task.orchestrator.redecomposer")


def _draft_scalar_to_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _StepDraft(BaseModel):
    """One DSL step in flat, LLM-friendly form. `op` selects which fields matter."""

    op: str = Field(default="run", description='"run" | "if" | "finish" | "foreach" | "compute" | "call"')
    # --- op=run ---
    var: str = Field(default="", description="把该步结果绑定到的变量名；带 returns/data_query 或后续要引用时填，否则留空")
    name: str = Field(default="", description="op=run：该 milestone 的一句话操作指令")
    success_condition: str = Field(default="", description="op=run：完成后界面应处于的唯一可截图确认终态")
    run_kind: str = Field(
        default="action",
        description=(
            "op=run：先判执行模式再选 kind。交互命令=navigation/filter/action（会改变页面、设置控件、"
            "点击/提交/保存，或进入 GUI/浏览器执行边界）；非交互语句=read/data_query（解释器本地读取/查询，"
            "不能点击、填写、保存、提交）。"
        ),
    )
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
    return_domains: dict[str, str] = Field(
        default_factory=dict,
        description="op=run 可选：returns 字段的取值域声明 {字段: 域}，域 ∈ url | number | date | "
                    "enum:值1|值2|... | text。运行时用它拒收出域返回值（读到垃圾→重试定位，不静默给错答）；"
                    "枚举判定类字段（成功/失败、是/否、几种状态之一）强烈建议声明 enum 域。",
    )
    sql: str = Field(
        default="",
        description=(
            "op=run 且 run_kind=data_query：只读 SQL。只能 SELECT/WITH SELECT；默认表名 data，"
            "列名为表头 snake_case；金额/数字/百分比显示文本可用 <column>_num，日期时间显示文本可用 <column>_ts。"
        ),
    )
    data_scope: str = Field(
        default="complete",
        description='op=run 且 run_kind=data_query：complete | current。complete=要求完整数据；current=只分析当前页面/当前可见/当前已渲染行。',
    )
    # --- op=foreach（通用迭代：对某个列表型 read 的每一行跑一遍 body）---
    loop_var: str = Field(default="", description="op=foreach：循环变量名，body 里用 {循环变量[字段]} 引用当前行")
    over: str = Field(default="", description="op=foreach：被迭代的列表来源（旧路径，iPhone/Android 兼容）；browser 新路径留空，由 name/row_fields 驱动")
    into: str = Field(default="", description="op=foreach：累积表变量名（留空默认 = 循环变量+s）；循环结束后可被 data_query 查询")
    row_fields: list[str] = Field(
        default_factory=list,
        description="op=foreach：从当前列表/网格每一行先采集并绑定到 loop_var 的字段（如 sku/product_name/detail_url/current_price）。"
                    "body/body_goal 里用 {loop_var[字段]} 引用这些字段。新计划优先使用本字段；旧计划可继续用 returns 表示行采集字段。",
    )
    output_fields: list[str] = Field(
        default_factory=list,
        description="op=foreach：循环结束后 into 表承诺产出的字段。使用 body_goal 时必须列出每行子目标最终返回/计算/保存后的字段"
                    "（如 size/old_price/new_price/status）；后续 data_query 只能查询 row_fields + output_fields。"
                    "旧 body_goal 计划可继续用 returns 表示 output_fields。",
    )
    body: list["_StepDraft"] = Field(default_factory=list, description="op=foreach：每行执行一遍的步骤（run/if/finish，不可再嵌 foreach）")
    body_goal: str = Field(default="", description="op=foreach（逐行子目标，替代写死的 body）：当每行子任务太复杂、固定步骤拼不可靠（需从行字段派生键→搜索另一个关联实体→按谓词判别行→读其属性，即 per-row 实体 join），填一句含字面模板 {循环变量[字段]} 的子目标，运行时按行当场分解、由 agent 多跳完成；配 row_fields=子目标需要的行字段、output_fields=每行产出契约。与 body 互斥；子目标内不得再用 body_goal。例：'由 {row[entity_key]} 派生父实体标识→搜索关联父实体→打开匹配行→读取目标属性 attr→返回 attr'")
    member_desc: str = Field(
        default="",
        description=(
            "op=foreach：成员圈选描述。用于 member_desc + 显式 body 的渐进式编排："
            "当每行操作步骤是机械确定的，但哪些行属于目标集合要看运行时真实行数据时填写。"
            "运行时先按该语义描述圈选成员，只对成员执行 body。只写语义（如 'size 28 的 Sahara leggings 变体'），"
            "不要写猜测的 SKU/name LIKE 谓词。"
        ),
    )
    limit: int | None = Field(default=None, description="op=foreach：采集行数上限（None=全量）；对已排序 grid 取 topK 时填 K，避免全量翻页")
    # --- op=compute（纯计算：解释器确定性求值，不是 GUI milestone；用于从已有标量派生新值）---
    expr: str = Field(default="", description="op=compute：受限表达式，对作用域内标量（函数参数 + 之前的 compute 结果，用裸名引用）求值，结果绑到 var（标量，后续 milestone/参数里用 {var} 引用）。只允许：字符串方法(rsplit/split/strip/replace/lower…)、切片/索引、`+`、以及 re_sub(pattern,repl,s)/re_search(pattern,s)/len/str/int。例：re_sub('-[^-]+$','',entity_key) 去掉最后一段后缀得到父实体标识；entity_key.rsplit('-',1)[0] 同理。**派生类计算用它，别塞进 milestone 让 agent 现场算。**")
    # --- op=call（调用一个函数定义；可出现在 main / if / foreach body 任意处，函数与循环无关）---
    func: str = Field(default="", description="op=call：要调用的函数名（必须是 functions 里定义过的）")
    call_args: dict[str, str] = Field(default_factory=dict, description="op=call：参数名→取值模板（在调用处作用域解析：{row[字段]} 取当前行、{标量} 取标量、或字面量）。函数返回值绑到 var（一个 RunResult，后续用 {var[返回字段]} 引用）")
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

    @field_validator("cond_value", mode="before")
    @classmethod
    def _coerce_cond_value(cls, value: object) -> str:
        return _draft_scalar_to_str(value)

    @field_validator("cond_values", mode="before")
    @classmethod
    def _coerce_cond_values(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [_draft_scalar_to_str(item) for item in value]
        return [_draft_scalar_to_str(value)]


class _FunctionDraft(BaseModel):
    """A reusable function — a parameterized sub-program, decoupled from any loop. Decomposed ONCE
    with main (one LLM call, like writing a code file), called N times via op=call."""

    name: str = Field(default="", description="函数名（main 或别的步骤用 op=call 调用它）")
    params: list[str] = Field(default_factory=list, description="参数名列表；函数体里用 {参数名} 引用（参数是标量）")
    body: list[_StepDraft] = Field(default_factory=list, description="函数体步骤（run milestone / compute / call / if / finish）；与 main 一样写")
    returns: list[str] = Field(default_factory=list, description="函数对外返回的字段名（compute 标量名，或体内某 milestone returns 的字段名）")


class _PlanDraft(BaseModel):
    reasoning: str = Field(
        default="",
        description="先分析任务：要到哪些页、做什么操作、读什么结果、是否需要条件分支；再据此写 steps",
    )
    goal: str = Field(default="", description="任务一句话描述")
    functions: list[_FunctionDraft] = Field(
        default_factory=list,
        description="可复用函数定义（顶层，与循环无关）。当某个子过程要被 foreach 逐行调用、或在多处复用、"
                    "或本身是多跳实体查找（派生→搜→判别→读）时，把它写成一个函数，main 里用 op=call 调用。"
                    "整份程序（main steps + functions）一次产出，像写一个代码文件。",
    )
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
        elif op == "compute":
            if d.expr.strip() and d.var.strip():
                out.append(Compute(var=d.var.strip(), expr=d.expr.strip()))
        elif op == "call":
            if d.func.strip():
                out.append(Call(
                    func=d.func.strip(),
                    args={k.strip(): v for k, v in (d.call_args or {}).items() if k.strip()},
                    var=(d.var.strip() or None),
                ))
        elif op == "foreach":
            # One level only: drop any nested foreach in the body (decomposer prompt forbids it; this
            # is the deterministic backstop) so a malformed nested loop can't reach the interpreter.
            body = [s for s in _to_stmts(d.body) if not isinstance(s, ForEach)]
            body_goal = (getattr(d, "body_goal", "") or "").strip()
            out.append(ForEach(
                var=(d.loop_var or "item").strip(),
                over=d.over.strip(),
                target=d.name.strip(),
                returns=[r for r in d.returns if r.strip()],
                row_fields=[r for r in d.row_fields if r.strip()],
                output_fields=[r for r in d.output_fields if r.strip()],
                # body_goal (agentic per-row sub-goal) is mutually exclusive with body; when present
                # it wins and body is dropped (the runtime decomposes the sub-goal fresh per row).
                into=d.into.strip(),
                body=[] if body_goal else body,
                body_goal=body_goal,
                member_desc=(getattr(d, "member_desc", "") or "").strip(),
                limit=d.limit if d.limit and d.limit > 0 else None,
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
        else:  # run family (default): construction-time split by execution mode (S8 sibling IR).
            kind = _to_kind(d.run_kind)
            common = dict(
                var=(d.var.strip() or None),
                name=d.name,
                success_condition=d.success_condition,
                returns=[r for r in d.returns if r.strip()],
                read_spec=d.read_spec,
            )
            if kind == "read":
                # 非交互 frame read：只有共享形状字段（precondition/return_domains 是
                # 交互专属概念，确定性丢弃）。
                out.append(Read(**common))
            elif kind == "data_query":
                out.append(Query(
                    **common,
                    sql=d.sql,
                    data_scope="current" if (d.data_scope or "").strip().lower() == "current" else "complete",
                ))
            else:
                out.append(Run(
                    **common,
                    kind=kind,  # type: ignore[arg-type]  # narrowed to the interactive literals
                    # Deterministic normalization: keep only domains whose field is actually declared
                    # in returns (an unknown key is decomposer noise, not a contract).
                    return_domains={
                        k.strip(): v.strip()
                        for k, v in (d.return_domains or {}).items()
                        if k.strip() and v.strip() and k.strip() in {r.strip() for r in d.returns}
                    },
                    precondition=bool(d.precondition),
                ))
    return out


def _to_functions(drafts: list["_FunctionDraft"]) -> list[FunctionDef]:
    """Convert function drafts → FunctionDef. One level of function bodies; nested foreach in a
    body is still dropped by _to_stmts' foreach handling, but functions themselves may call others."""
    out: list[FunctionDef] = []
    for fd in drafts or []:
        name = (fd.name or "").strip()
        if not name:
            continue
        out.append(FunctionDef(
            name=name,
            params=[p.strip() for p in fd.params if p.strip()],
            body=_to_stmts(fd.body),
            returns=[r.strip() for r in fd.returns if r.strip()],
        ))
    return out


def to_program(draft: _PlanDraft, goal: str) -> Program:
    """Draft → AST + STRUCTURAL passes (collapse / insert-loop-arrival / chain_from_states).

    Deliberately does NOT run the gate normalizations (confirm-read / precondition): those rewrite
    a step's kind/success_condition and would defeat validator rules that key on what the LLM
    actually wrote (e.g. "table-row fields on a filter's returns" — the confirm-read pass converts
    that filter to an action, so validating AFTER it would silently swallow the guidance). Validate
    sees this output; gate normalization is a separate post-validation finalize — see
    passes.finalize_gates, applied once by decompose/redecompose (not re-wrapped per call site)."""
    from .passes import chain_from_states, collapse_foreach_enrichment_passes, insert_loop_entry_arrivals

    return chain_from_states(insert_loop_entry_arrivals(collapse_foreach_enrichment_passes(Program(
        goal=draft.goal or goal,
        statements=_to_stmts(draft.steps),
        functions=_to_functions(getattr(draft, "functions", []) or []),
    ))))








































































_MAX_RETRIES = 2


class OrchestratorCompileError(RuntimeError):
    """Raised when LLM draft repair retries are exhausted with validator issues still present."""

    def __init__(self, issues: list[ValidationIssue], program: Program) -> None:
        self.issues = list(issues)
        self.program = program
        joined = "; ".join(str(issue) for issue in self.issues[:3])
        suffix = "" if len(self.issues) <= 3 else f"; ... (+{len(self.issues) - 3})"
        super().__init__(f"orchestrator compile validation failed: {joined}{suffix}")


def _contract_issue_to_validation_issue(issue: IntentContractIssue) -> ValidationIssue:
    return ValidationIssue(
        issue.code,
        issue.message,
        severity=issue.severity,
        evidence=issue.evidence,
    )


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
    attempt_observer: "Callable[[int, list[ValidationIssue]], None] | None" = None,
    resolution: "IntentResolution | None" = None,
) -> Program:
    """Shared LLM call + deterministic validate/feedback-retry. Both decompose() and redecompose()
    assemble their own context blocks, then hand off here for the identical draft→AST→validate loop.
    `resolution` (when the caller has one) additionally arms intent-contract checks
    (router entity coverage / set selector membership / entity-scope predicates) so all generation
    entrances share the same repair feedback."""
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    issues: list[ValidationIssue] = []
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
        program = _normalize_data_query_display_identifiers(program)
        issues = list(validate_program(program))
        if resolution is not None:
            issues.extend(
                _contract_issue_to_validation_issue(issue)
                for issue in validate_intent_contracts(program, resolution)
            )
        if attempt_observer is not None:
            # Offline instrumentation only (default None ⇒ production path unchanged): record the
            # codes that fired on each draft so the retry-efficacy harness can measure, per code,
            # whether feeding it back actually clears it on the next attempt. See
            # scripts/validator_retry_efficacy.py.
            attempt_observer(attempt, list(issues))
        if not issues:
            break
        if attempt < _MAX_RETRIES:
            print(f"  [Orchestrator] 程序分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{_MAX_RETRIES})...")
            for i in issues:
                print(f"  [Orchestrator]   {i}")
    if issues:
        print(f"  [Orchestrator] 程序分解校验仍有 {len(issues)} 项问题，停止出厂。")
        for i in issues:
            print(f"  [Orchestrator]   {i}")
        raise OrchestratorCompileError(issues, program)
    return program


def _normalize_data_query_display_identifiers(program: Program) -> Program:
    """Rewrite quoted UI display labels in SQL to known normalized table identifiers.

    Runtime data_query already maps structured-table display headers to normalized SQL
    identifiers. Do the same deterministic rewrite at compile time for tables that the
    program itself declares via foreach row_fields/returns/output_fields, so validation feedback
    does not get stuck on harmless display-label quoting.
    """
    updated = program.model_copy(deep=True)

    def _field_headers_from_body(stmts: list[Stmt]) -> list[str]:
        headers: list[str] = []
        for item in stmts:
            if isinstance(item, RunLike):
                headers.extend(item.returns or [])
            elif isinstance(item, Compute):
                headers.append(item.var)
            elif isinstance(item, If):
                headers.extend(_field_headers_from_body(item.then))
                headers.extend(_field_headers_from_body(item.otherwise))
            elif isinstance(item, ForEach):
                headers.extend(item.row_fields or item.returns or [])
                headers.extend(item.output_fields or [])
        return _unique_headers(headers)

    def _unique_headers(headers: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for header in headers:
            text = str(header or "").strip()
            key = _sql_identifier(text)
            if not text or key in seen:
                continue
            out.append(text)
            seen.add(key)
        return out

    def _walk(stmts: list[Stmt], tables: list[dict]) -> None:
        local_tables = list(tables)
        for stmt in stmts:
            if isinstance(stmt, RunLike) and stmt.kind == "data_query" and stmt.sql:
                stmt.sql = _rewrite_quoted_display_identifiers(stmt.sql, local_tables)
            elif isinstance(stmt, ForEach):
                headers = _unique_headers([
                    *(stmt.row_fields or stmt.returns or []),
                    *(stmt.output_fields or []),
                    *_field_headers_from_body(stmt.body),
                ])
                caption = stmt.into or f"{stmt.var}s"
                if caption and headers:
                    local_tables.append({"caption": caption, "headers": headers, "rows": []})
                _walk(stmt.body, list(local_tables))
            elif isinstance(stmt, If):
                _walk(stmt.then, list(local_tables))
                _walk(stmt.otherwise, list(local_tables))
            elif isinstance(stmt, Call):
                continue

    _walk(updated.statements, [])
    return updated


def _iter_runs(stmts: list[Stmt]):
    for stmt in stmts:
        if isinstance(stmt, RunLike):  # whole run family — the SQL rewrite targets Query nodes
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
    attempt_observer: "Callable[[int, list[ValidationIssue]], None] | None" = None,
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
        attempt_observer=attempt_observer,
        resolution=resolution,
    )
    from .passes import finalize_gates
    return finalize_gates(_normalize_approximate_entity_sql(program, resolution))


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
        resolution=resolution,
    )
    from .passes import finalize_gates
    return finalize_gates(_normalize_approximate_entity_sql(program, resolution))


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
        typed_shadows = _schema_typed_shadow_candidates(headers, sql_columns)
        column_text = ", ".join(sql_columns) if sql_columns else "(no headers)"
        typed_text = f"; typed shadows if parseable: {', '.join(typed_shadows)}" if typed_shadows else ""
        labels_text = f"; source labels: {', '.join(labels)}" if labels else ""
        lines.append(
            f"- {'/'.join(aliases)};{caption_text} sql columns: {column_text}{typed_text}{labels_text}; rows: {row_text}; {completeness}"
        )
    if not lines:
        return ""
    return (
        "\n## 当前结构化表格（仅 schema，不含行数据）\n"
        "这些表格来自当前界面已采集的表格快照；用于规划 data_query 的表名和列名。"
        "这里故意不提供行数据，实际查询由受限 SQLite primitive 在运行时读取。\n"
        + "\n".join(lines)
        + "\n若这些表格已经是任务要求的数据源终态，可生成 data_query；否则先规划导航、筛选/搜索/排序、清除旧筛选或完整采集步骤。SQL 只能使用表名、sql columns 中列出的 snake_case 标识符，以及运行时可解析的 typed shadows。source labels 只是人类可读说明，不是 SQL 语法。"
    )


def _schema_typed_shadow_candidates(headers: list[Any], columns: list[str]) -> list[str]:
    shadows: list[str] = []
    numeric_hints = (
        "amount", "total", "price", "cost", "qty", "quantity", "count", "number", "score",
        "rating", "percent", "uses", "results", "subtotal", "tax", "shipping", "payment",
        "paid", "grand", "%",
    )
    datetime_hints = ("date", "time", "created", "updated", "purchased", "ordered", "posted")
    for header, column in zip(headers, columns):
        text = f"{header} {column}".lower()
        if any(hint in text for hint in datetime_hints):
            shadows.append(f"{column}_ts")
        if any(hint in text for hint in numeric_hints):
            shadows.append(f"{column}_num")
    return shadows[:24]
