"""LLM draft schema and AST conversion for the orchestrator decomposer."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from gui_agent.core.schemas import TargetValue, target_value_options

from ..program import (
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
    assign_statement_ids,
)


def _draft_scalar_to_str(value: object, *, python_bool: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        if python_bool:
            return "True" if value else "False"
        return "true" if value else "false"
    return str(value)


class _StepDraft(BaseModel):
    """One DSL step in flat, LLM-friendly form. `op` selects which fields matter."""

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_effect_mode(cls, data: object) -> object:
        if isinstance(data, dict) and "effect_mode" in data:
            raise ValueError(
                "effect_mode is retired; declare target_values for state contracts"
            )
        return data

    op: str = Field(default="run", description='"run" | "if" | "finish" | "foreach" | "compute" | "call"')
    # --- op=run ---
    var: str = Field(default="", description="把该步结果绑定到的变量名；带 returns/data_query 或后续要引用时填，否则留空")
    name: str = Field(
        default="",
        description=(
            "op=run：该 interactive run 的一句话语义指令。action 写一次持久化业务状态变更，不写展开、"
            "选择、继续、保存等向导手势序列；这些是该 run 内部的执行细节。"
        ),
    )
    success_condition: str = Field(
        default="",
        description=(
            "op=run：完成后界面应处于的唯一可截图确认终态。action 必须写业务状态已经持久化后的终态；"
            "弹出面板、进入向导、出现下一步按钮等只表示过程开始，不能作为独立 action 的终态。"
        ),
    )
    run_kind: str = Field(
        default="action",
        description=(
            "op=run：先判执行模式再选 kind。navigation=到达列表/详情/编辑器/管理页或页内区域，"
            "即使需要点击入口也仍是 navigation；filter=应用检索/筛选状态；action=完成一个独立资源的"
            "一次持久化业务状态变更，向导内部手势不拆 action；read/data_query=解释器本地读取/查询，"
            "不能点击、填写、保存、提交。"
        ),
    )
    precondition: bool = Field(
        default=False,
        description="op=run：该步是否为【前置状态保障】（确保已登录 / 已进入某模式或某页，初始往往已满足）。"
                    "是→true 且 run_kind 用 navigation；普通去某页/做某操作就留 false。",
    )
    covers_set: str = Field(
        default="",
        description="op=run 可选：当任务实体是【多目标(一组)】、但应用知识明确指出这组成员由单一聚合对象/"
                    "批量机制一次覆盖（父记录一次保存级联全部子记录、全选+批量动作、bulk edit）时，"
                    "在执行该聚合动作的 mutation 步上填被覆盖的实体提及原文，程序可以不用 foreach。"
                    "Router 给出 collection_scope 且本步是其聚合 owner 时，必须把该 scope 原文填在这里。"
                    "没有知识依据时禁止填写——那等于漏改其余成员。",
    )
    persistence: str = Field(
        default="immediate",
        description="run_kind=action：immediate | explicit_commit。",
    )
    target_controls: list[str] = Field(
        default_factory=list,
        description=(
            "op=run 且为交互步骤：该步必须命中的字段、控件或集合能力名称。"
            "例如命名列筛选填写列名；mutation 填业务字段或集合名，不写 CSS/坐标。"
        ),
    )
    target_values: dict[str, TargetValue] = Field(
        default_factory=dict,
        description=(
            "run_kind=action：本步必须实现的业务终态；同一选择组需要多个值时使用字符串数组，"
            "不得拼成 and/和 连接的单个字符串；重复的非选择控件组可用多个字段的等长数组，"
            "数组同一位置共同声明一行，禁止不同长度。"
            "Router 的 target_value 可作为写入目标并在知识明确要求时建立定义前置；qualifier_value"
            "只能进入最终 mutation 的选择合同，禁止为它建立独立的创建/改写阶段。"
            "run_kind=filter：完成后的完整筛选状态，"
            "每个字段只声明一个已应用值。它不提供目标身份或写入授权；"
            "不得写按钮、选择器、坐标或系统生成的 ID/时间戳。"
        ),
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
            "运行时先按该语义描述圈选成员，只对成员执行 body。只写集合成员的业务语义，"
            "不要写猜测的 SKU/name LIKE 谓词。"
        ),
    )
    limit: int | None = Field(default=None, description="op=foreach：采集行数上限（None=全量）；对已排序 grid 取 topK 时填 K，避免全量翻页")
    # --- op=compute（纯计算：解释器确定性求值，不进入 GUI；用于从已有标量派生新值）---
    expr: str = Field(default="", description="op=compute：受限 Python 表达式，对作用域内标量（函数参数 + 之前的 compute 结果，用裸名引用）求值，结果绑到 var（标量，后续 step/参数里用 {var} 引用）。字段值用 q['field'] 或 {q[field]} 参与表达式并显式拼接；不要写 '{q[field]} text' 这种模板字符串/f-string。字符串里含单引号/apostrophe 时用双引号或转义。只允许：字符串方法(rsplit/split/strip/replace/lower…)、切片/索引、算术/比较/三元、re_sub/re_search/len/str/int/float/round/abs；禁止 list comprehension、generator、next(row ... for row in rows)、__import__、SQLAlchemy/engine/cursor 等集合遍历或外部执行，选行/计数/聚合/SQL 必须用 data_query。例：re_sub('-[^-]+$','',entity_key) 去掉最后一段后缀得到父实体标识；entity_key.rsplit('-',1)[0] 同理。派生计算用 compute，不要塞进 interactive run 让执行层现场推导。")
    # --- op=call（调用一个函数定义；可出现在 main / if / foreach body 任意处，函数与循环无关）---
    func: str = Field(default="", description="op=call：要调用的函数名（必须是 functions 里定义过的）")
    call_args: dict[str, str] = Field(default_factory=dict, description="op=call：参数名→取值模板（在调用处作用域解析：{row[字段]} 取当前行、{标量} 取标量、或字面量）。函数返回值绑到 var（后续用 {var[返回字段]} 引用）")
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
        # The model occasionally emits a bare JSON scalar where a string field is declared — a
        # numeric cond_value. Coerce it so the PRIMARY json_object parse still validates instead of
        # dropping to the slow plain-text fallback.
        return _draft_scalar_to_str(value)

    @field_validator("expr", mode="before")
    @classmethod
    def _coerce_expr(cls, value: object) -> str:
        # Compute expressions are Python-surface expressions. If structured output sends a JSON bool
        # (expr: false), preserve that scalar as the Python literal False rather than converting it
        # to the unknown name "false" and trapping the repair loop in a language-boundary error.
        return _draft_scalar_to_str(value, python_bool=True)

    @field_validator("cond_values", mode="before")
    @classmethod
    def _coerce_cond_values(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [_draft_scalar_to_str(item) for item in value]
        return [_draft_scalar_to_str(value)]


class _FunctionDraft(BaseModel):
    """A reusable function: a parameterized step sequence, decoupled from any loop."""

    name: str = Field(default="", description="函数名（main 或别的步骤用 op=call 调用它）")
    params: list[str] = Field(default_factory=list, description="参数名列表；函数体里用 {参数名} 引用（参数是标量）")
    body: list[_StepDraft] = Field(default_factory=list, description="函数体步骤（run / compute / call / if / finish）；与 main 一样写")
    returns: list[str] = Field(default_factory=list, description="函数对外返回的字段名（compute 标量名，或体内某 run returns 的字段名）")


class _PlanDraft(BaseModel):
    reasoning: str = Field(
        default="",
        description=(
            "先列出任务涉及的持久化资源、各资源的所有者类型、明确的资源依赖，并拓扑排序完整资源阶段；"
            "值依赖必须服从 Router 角色：只有 target_value 可产生定义前置，qualifier_value 只能被最终"
            "mutation 选择，不能成为独立持久化资源；"
            "再分析要到哪些页、做什么操作、读什么结果、是否需要条件分支。若采集所有者判别字段，必须在"
            "选唯一目标的 member_desc、if 条件或 data_query WHERE 中实际消费。向既有 owner 的集合增加"
            "member 时，先用 navigation/filter 定位并打开既有 owner，再执行成员集合 mutation；不得误建"
            "一个同名 owner。最后再写 steps。"
        ),
    )
    goal: str = Field(default="", description="任务一句话描述")
    functions: list[_FunctionDraft] = Field(
        default_factory=list,
        description="可复用函数定义（顶层，与循环无关）。当某个子过程要被 foreach 逐行调用、或在多处复用、"
                    "或本身是多跳实体查找（派生→搜→判别→读）时，把它写成一个函数，main 里用 op=call 调用。"
                    "整份 DSL 输出（main steps + functions）一次产出。",
    )
    steps: list[_StepDraft] = Field(default_factory=list)


_StepDraft.model_rebuild()

_VALID_KINDS = {"navigation", "filter", "action", "read", "data_query"}
_VALID_CMPS = {"==", "!=", "exists", "empty", "contains", "not_contains", "in", "not_in"}
_URL_OPEN_TEMPLATE_RE = re.compile(
    r"\{[^{}]*(?:url|href|link|链接|网址)[^{}]*\}|\{url\}",
    re.IGNORECASE,
)
_URL_OPEN_VERB_RE = re.compile(
    r"打开|进入|前往|访问|跳转|到达|\b(open|navigate|go to|visit|view|edit)\b",
    re.IGNORECASE,
)
_URL_VALUE_MUTATION_RE = re.compile(
    r"设置|填入|填写|输入|更新|保存|提交|\b(set|fill|type|update|save|submit)\b",
    re.IGNORECASE,
)
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


def _normalize_run_kind(kind: RunKind, name: str) -> RunKind:
    """URL-capability opens are page navigation even if the model labels them as action/filter."""
    text = name or ""
    if (
        kind in {"action", "filter"}
        and _URL_OPEN_TEMPLATE_RE.search(text)
        and _URL_OPEN_VERB_RE.search(text)
        and not _URL_VALUE_MUTATION_RE.search(text)
    ):
        return "navigation"
    return kind


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
            kind = _normalize_run_kind(_to_kind(d.run_kind), d.name)
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
                    covers_set=(d.covers_set or "").strip(),
                    persistence=(
                        "explicit_commit"
                        if (d.persistence or "").strip().lower() == "explicit_commit"
                        else "immediate"
                    ),
                    target_controls=[
                        value.strip()
                        for value in (d.target_controls or [])
                        if value.strip()
                    ],
                    target_values={
                        str(key).strip(): (
                            list(options) if isinstance(value, list) else options[0]
                        )
                        for key, value in (d.target_values or {}).items()
                        if str(key).strip()
                        if (options := target_value_options(value))
                    },
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
    from ..passes import (
        bind_singleton_query_urls,
        chain_from_states,
        collapse_foreach_enrichment_passes,
        insert_loop_entry_arrivals,
    )

    program = Program(
        goal=draft.goal or goal,
        statements=_to_stmts(draft.steps),
        functions=_to_functions(getattr(draft, "functions", []) or []),
    )
    program = collapse_foreach_enrichment_passes(program)
    program = bind_singleton_query_urls(program)
    program = insert_loop_entry_arrivals(program)
    return assign_statement_ids(chain_from_states(program))
