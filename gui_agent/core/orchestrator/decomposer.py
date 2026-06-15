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

import base64

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.policies.base import resize_to_logical_png
from llm.structured import invoke_structured

from .program import Cond, Finish, If, Program, Run, RunKind, Stmt

_SYSTEM = """\
你是 GUI 自动化任务的【编排器分解器】。把用户任务分解成一段小程序（DSL steps），由解释器按顺序执行。

输出 steps：每个 step 是三种之一，用 op 字段区分：
- op="run"：驱动一个 milestone（一个线性 GUI 子任务）。
    · 粒度 = 到达某页 / 填一组表单 / 点一个按钮 / 读取一个结果——**不是整个任务，也不是单次点击**；多步导航合并成一个 run。
    · name：该 milestone 的一句话操作指令。
    · run_kind：navigation（到达/打开某页面，不改状态）| filter（设搜索/筛选条件）| action（执行一次改变状态的操作：提交/发送/创建/删除/设置）| read（只读取结果，不做任何操作）。
    · success_condition：完成后界面应处于的【唯一可截图确认】终态——写终态不写增量；action 类写操作生效后的可见结果（成功提示/结果页），不是「按钮可见」「已聚焦」。**例外：若该 action 后面跟了 read 判结果（confirm-read，见规则6），它的验收只写「动作已发出」（按钮已点、表单已提交、出现响应/进入计算·加载，且明确不判结果的具体取值），把结果判读让给那个 read——别让验收门和 read 判同一个东西。**
    · var：把该步结果绑定到变量，仅当后续 if / finish 要引用它时填（通常只有 read 步需要）。
    · returns：仅 run_kind="read" 填——要从结果界面读取的字段名列表，程序据此判断分支。
    · read_spec：仅 run_kind="read" 填——【本次读取说明】，按任务需求生成：逐个说明每个 returns 字段在结果界面上看哪里、如何把信号（图标/颜色/文字/位置）判读成值、各取值的含义（例：「连通判定：看起点终点输入框之间的图标——绿色✓=连通，灰色?=未检测/未连通；不可达原因：连通时为空，不可达时读取页面上的红色错误提示文字」）。读取是只读单帧，没有这份说明就只能瞎猜，所以必须写清楚。
- op="if"：按某个 read 步读到的字段值分支。cond_var=那个 read 步的 var；cond_field=该步 returns 里的字段；cond_cmp= "==" 或 "!="；cond_value=期望值；then=成立时执行的步骤；otherwise=不成立时执行的步骤。
- op="finish"：产出最终答复。message 是模板，可用 {变量[字段]} 引用某 read 步读到的值。

核心原则：
1. milestone 粒度：「搜关键词并进第一条结果」= 一个 navigation run，别拆成 输入→提交→点结果。
2. 只在任务真有「读结果 → 据结果决定下一步」时才用 read + if；纯线性任务直接顺序 run，结尾可选 finish。
3. read 是只读单帧数据提取：它不点任何东西、不做验收（读不到=当没有），只把 returns 字段读出来。**「触发某结果的操作」和「读取该结果」必须分成两步**——先用 action 触发，再用 read 读取。
4. 验收终态且有出处：只用任务、@引用文件或截图里出现的值，不编造系统生成的编号/名称（用特征描述）。
5. 能一句话答复就用 finish 模板引用 read 值；不写 finish 时解释器会自动汇总各 read 结果。
6. **关键动作 confirm-read（成败由结构化读取定，别只信动作完成）**：会改变状态的关键动作（创建/提交/删除/发送/设置/检测查询，尤其任务的最终动作），在该 action 之后补一个 read 确认结果——returns 含一个成败/状态字段，read_spec 写清「成功看什么信号、失败看什么信号」；再由 finish（或 if）据这个结构化结果答复/分支。不要只凭动作步自身被判完成就当任务成功。配套地（规则4例外）：该 action 的 success_condition 写「动作已发出」而非「结果已显示/某判定已出现」，结果的具体判读独占给这个 read——验收门去判结果会反复纠结一个刚出现/会消失的小图标（看到却不信→重复触发，或把同一个标志判读漂移），正是 confirm-read 要绕开的。

只输出与任务相关的步骤，不加多余前置（已在工作区就别加「打开网站」）。先在 reasoning 里想清楚：要到哪些页、做什么操作、读什么结果、关键动作做完怎么确认、是否需要分支，再写 steps。

示例（条件任务 + confirm-read）——
{"reasoning":"先进路线规划页(navigation)，填起终点触发检测(action)，读连通结果(read，字段=是否可达)，据此分支：可达则创建行程、创建后 confirm-read 确认再答复，不可达则直接答复。","goal":"查询 A 到 B 是否可达，可达则创建行程","steps":[
 {"op":"run","run_kind":"navigation","name":"进入路线规划页","success_condition":"页面显示起点/终点输入框"},
 {"op":"run","run_kind":"action","name":"填入起点 A、终点 B 并触发路径检测","success_condition":"已点击检测、起点终点间出现结果响应（连通与否由后续 read 判读，验收不判具体取值）"},
 {"op":"run","run_kind":"read","var":"r","name":"读取检测结果","returns":["是否可达","不可达原因"],"read_spec":"是否可达：看起点终点之间的连通图标——绿色✓判为「可达」，灰色?或红色×判为「不可达」；不可达原因：可达时留空，不可达时读取页面红色错误提示文字。"},
 {"op":"if","cond_var":"r","cond_field":"是否可达","cond_cmp":"==","cond_value":"可达",
  "then":[
    {"op":"run","run_kind":"action","name":"创建该行程","success_condition":"已提交创建（弹出提示或返回列表，成败由后续 read 判读）"},
    {"op":"run","run_kind":"read","var":"c","name":"确认行程已创建","returns":["创建结果"],"read_spec":"创建结果：行程/订单列表出现该条目、或弹出「创建成功」提示，判「成功」；否则（仍停在表单、出现红色错误、列表无新条目）判「失败」。"},
    {"op":"finish","message":"已为 A 到 B 创建行程：{c[创建结果]}"}
  ],
  "otherwise":[{"op":"finish","message":"A 到 B 不可达：{r[不可达原因]}"}]}]}
"""


class _StepDraft(BaseModel):
    """One DSL step in flat, LLM-friendly form. `op` selects which fields matter."""

    op: str = Field(default="run", description='"run" | "if" | "finish"')
    # --- op=run ---
    var: str = Field(default="", description="把该步结果绑定到的变量名；仅 read 或后续要引用时填，否则留空")
    name: str = Field(default="", description="op=run：该 milestone 的一句话操作指令")
    success_condition: str = Field(default="", description="op=run：完成后界面应处于的唯一可截图确认终态")
    run_kind: str = Field(default="action", description='op=run：navigation | filter | action | read')
    returns: list[str] = Field(default_factory=list, description="op=run 且 run_kind=read：要读取的结果字段名列表")
    read_spec: str = Field(
        default="",
        description="op=run 且 run_kind=read：本次读取说明——逐个说明每个 returns 字段在界面上看什么、"
                    "如何把信号(图标/颜色/文字/位置)判读成值、各取值含义；让纯只读能据此判读。",
    )
    # --- op=if ---
    cond_var: str = Field(default="", description="op=if：条件依据的变量名（某个 read 步的 var）")
    cond_field: str = Field(default="", description="op=if：读取字段名（该 read 步 returns 里的字段）")
    cond_cmp: str = Field(default="==", description='op=if："==" 或 "!="')
    cond_value: str = Field(default="", description="op=if：期望值，与读取到的字段值比较")
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

_VALID_KINDS = {"navigation", "filter", "action", "read"}


def _to_kind(raw: str) -> RunKind:
    k = (raw or "").strip().lower()
    return k if k in _VALID_KINDS else "action"  # type: ignore[return-value]


def _to_stmts(drafts: list[_StepDraft]) -> list[Stmt]:
    """Deterministically convert flat step drafts into the clean Program AST."""
    out: list[Stmt] = []
    for d in drafts:
        op = (d.op or "run").strip().lower()
        if op == "finish":
            out.append(Finish(message=d.message))
        elif op == "if":
            out.append(
                If(
                    cond=Cond(
                        var=d.cond_var,
                        field=d.cond_field,
                        cmp="!=" if d.cond_cmp.strip() == "!=" else "==",
                        value=d.cond_value,
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
                )
            )
    return out


def to_program(draft: _PlanDraft, goal: str) -> Program:
    return Program(goal=draft.goal or goal, statements=_to_stmts(draft.steps))


def _read_fields(stmts: list[Stmt]) -> dict[str, set[str]]:
    """Map each read run's var -> the fields it returns (walks into branches)."""
    fields: dict[str, set[str]] = {}
    for s in stmts:
        if isinstance(s, Run) and s.var and s.kind == "read":
            fields[s.var] = set(s.returns)
        elif isinstance(s, If):
            fields.update(_read_fields(s.then))
            fields.update(_read_fields(s.otherwise))
    return fields


def validate_program(program: Program) -> list[str]:
    """Deterministic shape guards — the high-value ones for the conditional read pattern.

    An if must branch on a field a prior read actually returns (else the cond reads ""),
    a read must request fields (else it reads nothing), a finish {var[field]} ref must
    resolve. Returns human-readable issues fed back to the LLM for one repair pass."""
    issues: list[str] = []
    if not program.statements:
        return ["程序为空：至少要有一个 run 步骤"]
    reads = _read_fields(program.statements)

    def _walk(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, Run):
                if s.kind == "read" and not s.returns:
                    issues.append(f"read 步「{s.name}」没有 returns 字段——read 必须指定要读取的字段")
                if s.kind == "read" and not s.var:
                    issues.append(f"read 步「{s.name}」没有绑定 var——读到的结果无法被后续引用")
            elif isinstance(s, If):
                if s.cond.var not in reads:
                    issues.append(
                        f"if 条件引用的变量「{s.cond.var}」不是任何 read 步的 var——条件将永远读到空值"
                    )
                elif s.cond.field not in reads[s.cond.var]:
                    issues.append(
                        f"if 条件字段「{s.cond.var}[{s.cond.field}]」不在该 read 步的 returns 里"
                    )
                _walk(s.then)
                _walk(s.otherwise)

    _walk(program.statements)
    return issues


_MAX_RETRIES = 2


def decompose(
    goal: str,
    *,
    png_bytes: bytes | None = None,
    knowledge: str = "",
    file_section: str = "",
    system_prompt: str = "",
) -> Program:
    """Decompose a user goal into a DSL Program via LLM + deterministic validate/retry.

    `png_bytes` (current screen) gives the planner page context; `knowledge` injects app
    navigation knowledge; `file_section` is the resolved content of any `@<path>` refs in the
    goal (config field values the spoken goal only points at — see resolve_file_refs);
    `system_prompt` overrides the default DSL prompt (platform tuning).
    """
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )

    parts: list[dict] = [{"type": "text", "text": f"用户任务：{goal}"}]
    if file_section:
        parts.append({"type": "text", "text": "\n" + file_section})
    if knowledge:
        parts.append({"type": "text", "text": f"\n## 应用导航知识\n{knowledge}"})
    if png_bytes:
        b64 = base64.b64encode(resize_to_logical_png(png_bytes)).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    issues: list[str] = []
    program = Program(goal=goal, statements=[])
    for attempt in range(_MAX_RETRIES + 1):
        user_parts = list(parts)
        if issues:
            fb = "\n".join(f"  - {i}" for i in issues)
            user_parts.append({"type": "text", "text": f"\n上一轮分解存在以下问题，请修正：\n{fb}"})
        messages = [
            SystemMessage(content=system_prompt or _SYSTEM),
            HumanMessage(content=user_parts),
        ]
        draft = invoke_structured(llm, messages, _PlanDraft)
        program = to_program(draft, goal)
        issues = validate_program(program)
        if not issues:
            break
        if attempt < _MAX_RETRIES:
            print(f"  [Orchestrator] 程序分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{_MAX_RETRIES})...")
            for i in issues:
                print(f"  [Orchestrator]   {i}")
    return program
