"""LLM draft schema and AST conversion for the orchestrator decomposer."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

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
    Stmt,
)


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
    """Convert function drafts -> FunctionDef. One level of function bodies; nested foreach in a
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
    """Draft -> AST + structural passes."""
    from .passes import chain_from_states, collapse_foreach_enrichment_passes, insert_loop_entry_arrivals

    return chain_from_states(insert_loop_entry_arrivals(collapse_foreach_enrichment_passes(Program(
        goal=draft.goal or goal,
        statements=_to_stmts(draft.steps),
        functions=_to_functions(getattr(draft, "functions", []) or []),
    ))))
