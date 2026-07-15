"""chain_from_states: FROM[i] := TO[i-1] — each statement's entry state is the prior statement's
success_condition, derived deterministically (not authored by the LLM). Underpins the FROM→TO
continuity that lets the decomposer phrase instructions without redundant "进入/回到 X" prefixes."""

from gui_agent.core.orchestrator import (
    Call,
    Compute,
    Finish,
    ForEach,
    FunctionDef,
    Program,
    Run,
    chain_from_states,
)
from gui_agent.core.orchestrator.program import Query, Read


def _prog() -> Program:
    return Program(
        goal="g",
        functions=[FunctionDef(
            name="resolve_parent_material", params=["sku"], returns=["material"], body=[
                Compute(var="base", expr="sku.rsplit('-',2)[0]"),
                Run(kind="filter", name="搜 {base}", success_condition="出现 SKU={base} 父产品行"),
                Run(kind="navigation", var="d", returns=["material"],
                    name="开 Edit 编辑页", success_condition="进入 {base} 编辑页，可见 Material"),
            ])],
        statements=[
            Run(kind="navigation", name="进入 Products", success_condition="显示产品列表与筛选控件"),
            Run(kind="filter", name="设 Quantity 3-3", success_condition="Active filters 显示 Quantity: 3 - 3"),
            ForEach(var="row", into="mats", returns=["Name", "SKU"], body=[
                Call(func="resolve_parent_material", args={"sku": "{row[SKU]}"}, var="m"),
            ]),
            Query( var="q", returns=["material"], name="去重", sql="SELECT DISTINCT material FROM mats"),
            Finish(message="材质：{q[material]}"),
        ],
    )


def test_main_sequence_chains_from_prior_success_condition():
    p = chain_from_states(_prog())
    runs = [s for s in p.statements if isinstance(s, Run)]
    # statements: [nav 进入Products, filter 设Quantity, (foreach), Query 去重] — 交互 Run 只有前两个
    assert runs[0].from_state == ""  # first step: block entry, unknown
    assert runs[1].from_state == "显示产品列表与筛选控件"  # FROM = prior nav's TO
    # the Query is a non-interactive statement: it doesn't even HAVE from_state (S8 sibling IR)
    query = next(s for s in p.statements if isinstance(s, Query))
    assert not hasattr(query, "from_state")


def test_function_body_chains_internally_first_step_empty():
    p = chain_from_states(_prog())
    body = p.functions[0].body
    filt = body[1]
    nav = body[2]
    # compute (body[0]) is page-neutral; the filter is the function's first Run → entry unknown
    assert filt.from_state == ""
    # the navigation's FROM is the filter's TO (search results showing) — NOT a redundant "回到 Products"
    assert nav.from_state == "出现 SKU={base} 父产品行"


def test_compute_is_page_neutral_from_carries_through():
    # A Compute between two Runs must not reset FROM: the Run after it still sees the prior Run's TO.
    prog = Program(goal="g", statements=[
        Run(kind="navigation", name="进入页", success_condition="在页面 X"),
        Compute(var="x", expr="1+1"),
        Run(kind="action", name="操作", success_condition="完成"),
    ])
    runs = [s for s in chain_from_states(prog).statements if isinstance(s, Run)]
    assert runs[1].from_state == "在页面 X"  # compute did not erase FROM


def test_call_advances_from_to_function_exit_state():
    # After a Call, the next statement's FROM is the called function's exit state (its last Run's SC).
    prog = Program(
        goal="g",
        functions=[FunctionDef(name="f", params=[], returns=["r"], body=[
            Run(kind="navigation", name="开详情页", success_condition="在详情页"),
        ])],
        statements=[
            Call(func="f", args={}, var="m"),
            Run(kind="navigation", name="下一步", success_condition="完成"),
        ],
    )
    runs = [s for s in chain_from_states(prog).statements if isinstance(s, Run)]
    assert runs[0].from_state == "在详情页"  # FROM = f's exit


def test_idempotent():
    once = chain_from_states(_prog())
    twice = chain_from_states(once)
    assert once.model_dump() == twice.model_dump()


def test_to_program_applies_chaining():
    # The production build path (to_program) must populate from_state, not just chain_from_states.
    from gui_agent.core.orchestrator.decomposer import _PlanDraft, _StepDraft, to_program

    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="navigation", name="进入页", success_condition="在页面 X"),
        _StepDraft(op="run", run_kind="action", name="操作", success_condition="完成"),
    ])
    prog = to_program(draft, "g")
    runs = [s for s in prog.statements if isinstance(s, Run)]
    assert runs[1].from_state == "在页面 X"


def test_query_runs_are_page_neutral_from_carries_through():
    """read/data_query 是非交互纯查询：不触界面、页面不变 —— FROM 链必须穿透（与 Compute 同款）。
    此前按普通 Run 处理：data_query 通常无 success_condition,会把后续 UI run 的 from_state 置空断链。"""
    prog = Program(goal="g", statements=[
        Run(kind="navigation", name="进入订单页", success_condition="在订单列表页"),
        Query( var="q", returns=["n"], name="数行", sql="SELECT COUNT(*) AS n FROM data"),
        Read( var="r", returns=["总数"], name="读页面计数"),
        Run(kind="action", name="点导出", success_condition="导出已触发"),
    ])
    stmts = chain_from_states(prog).statements
    runs = [s for s in stmts if isinstance(s, Run)]
    # 夹在中间的 Query + Read 都不改变页面 → action（交互 Run 的第 2 个）的 FROM 仍是导航的 TO
    assert runs[1].from_state == "在订单列表页"
    # 查询步自身不参与 FROM 标注（S8 平级 IR：非交互节点没有 from_state 字段）
    assert all(not hasattr(s, "from_state") for s in stmts if isinstance(s, (Read, Query)))


def test_function_exit_state_skips_trailing_query():
    """函数出口态 = 最后一个【命令】Run 的 SC;结尾的 read/data_query 不得把出口态清空。"""
    prog = Program(
        goal="g",
        functions=[FunctionDef(name="f", params=[], returns=["r"], body=[
            Run(kind="navigation", name="开详情页", success_condition="在详情页"),
            Read( var="r", returns=["r"], name="读取字段"),
        ])],
        statements=[
            Call(func="f", args={}, var="m"),
            Run(kind="navigation", name="下一步", success_condition="完成"),
        ],
    )
    runs = [s for s in chain_from_states(prog).statements if isinstance(s, Run)]
    assert runs[0].from_state == "在详情页"
