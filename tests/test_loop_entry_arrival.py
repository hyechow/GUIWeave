"""insert_loop_entry_arrivals: a loop/function body that opens by acting on a list (filter/action)
and then drills into a record (a later navigation) gets an idempotent "return to the list page"
arrival prepended — so foreach iteration 2+ (re-entering from the prior record's edit page) lands
back on the list before the search step, instead of trying to search on an edit page (live 185)."""

from gui_agent.core.orchestrator import (
    Call,
    Compute,
    ForEach,
    FunctionDef,
    Program,
    Run,
    insert_loop_entry_arrivals,
)
from gui_agent.core.orchestrator.passes import _ENTRY_ARRIVAL_SC
from gui_agent.core.orchestrator.program import Query


def _resolve_fn() -> FunctionDef:
    return FunctionDef(name="resolve", params=["sku"], returns=["material"], body=[
        Compute(var="base", expr="sku.rsplit('-',2)[0]"),
        Run(kind="filter", name="搜 {base}", success_condition="出现 SKU={base} 父产品行"),
        Run(kind="navigation", name="开 Edit 编辑页", success_condition="进入编辑页"),
    ])


def test_arrival_prepended_to_drilling_function_body():
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[_resolve_fn()], statements=[]))
    body = p.functions[0].body
    assert isinstance(body[0], Run) and body[0].kind == "navigation"
    assert body[0].success_condition == _ENTRY_ARRIVAL_SC
    # the original body follows intact
    assert isinstance(body[1], Compute)
    assert body[2].name == "搜 {base}"
    assert body[3].name == "开 Edit 编辑页"


def test_arrival_is_a_precondition_gate_not_a_branch_milestone():
    # The arrival is an entry-state navigation edge, not a business branch. Its text stays linear;
    # runtime, rather than checker prose, controls whether that edge has been traversed.
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[_resolve_fn()], statements=[]))
    arrival = p.functions[0].body[0]
    assert arrival.kind == "navigation" and arrival.precondition is True
    for branch_token in ("若", "则", "无需"):
        assert branch_token not in arrival.name
        assert branch_token not in arrival.success_condition


def test_no_arrival_when_body_stays_on_one_page():
    # filter then read on the SAME page (no later navigation) → re-entry == entry → no guard.
    fn = FunctionDef(name="f", params=[], returns=["x"], body=[
        Run(kind="filter", name="筛 X", success_condition="已筛"),
        Query( name="查", sql="SELECT 1"),
    ])
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[fn], statements=[]))
    assert p.functions[0].body[0].name == "筛 X"  # unchanged


def test_no_arrival_when_body_starts_with_plain_navigation():
    # A navigation that ARRIVES at a page (not a record drill) is left alone — it's already an arrival.
    fn = FunctionDef(name="f", params=[], returns=["x"], body=[
        Run(kind="navigation", name="进入 Catalog 设置页", success_condition="在设置页"),
        Run(kind="action", name="操作", success_condition="完成"),
    ])
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[fn], statements=[]))
    assert p.functions[0].body[0].name == "进入 Catalog 设置页"  # plain arrival nav → untouched


def test_arrival_prepended_when_first_step_drills_a_record_row():
    # Self-first 185 shape: the body's FIRST step is a navigation that DRILLS into a result row
    # (clicks SKU={sku} 那一行的 Edit) — no re-search. It inherently leaves the list, so foreach
    # iter2+ re-enters from the prior record's page → it needs the deterministic "回列表页" arrival.
    fn = FunctionDef(name="resolve", params=["sku"], returns=["material"], body=[
        Run(kind="navigation", var="self_d", returns=["material"],
            name="在当前结果列表点开 SKU={sku} 那一行的 Edit，进入它的详情页", success_condition="在 {sku} 详情页"),
        Run(kind="navigation", name="（else 里）开父详情页", success_condition="在父详情页"),
    ])
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[fn], statements=[]))
    body = p.functions[0].body
    assert body[0].kind == "navigation" and body[0].success_condition == _ENTRY_ARRIVAL_SC
    assert body[1].name.startswith("在当前结果列表点开")  # the drill follows the arrival


def test_no_arrival_when_first_step_opens_row_url():
    # URL direct-open is position-independent: it works from any page after templating, so adding a
    # generic list-arrival before it is unnecessary and can destroy the source filter context.
    fn = FunctionDef(name="resolve", params=["sku", "product_url"], returns=["material"], body=[
        Run(kind="navigation", var="self_d", returns=["material"],
            name="打开 {product_url}，进入 SKU={sku} 自己的产品详情页", success_condition="在 {sku} 详情页"),
    ])
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[fn], statements=[]))
    body = p.functions[0].body
    assert len(body) == 1
    assert body[0].name.startswith("打开 {product_url}")


def test_arrival_prepended_to_foreach_body_with_direct_runs():
    # a foreach body that itself filters then drills (no helper function) gets the guard too.
    prog = Program(goal="g", statements=[
        ForEach(var="row", into="out", returns=["X"], body=[
            Run(kind="filter", name="搜 {row[X]}", success_condition="出现结果"),
            Run(kind="navigation", name="开详情", success_condition="在详情页"),
        ]),
    ])
    body = insert_loop_entry_arrivals(prog).statements[0].body
    assert body[0].kind == "navigation" and body[0].success_condition == _ENTRY_ARRIVAL_SC
    assert body[1].name == "搜 {row[X]}"


def test_foreach_body_of_only_a_call_is_untouched():
    # 185's real shape: foreach body is just [call resolve] (the drill lives in the FUNCTION body,
    # which gets the guard) — the foreach body has no Run, so nothing is inserted here.
    prog = Program(
        goal="g", functions=[_resolve_fn()],
        statements=[ForEach(var="row", into="out", returns=["SKU"], body=[
            Call(func="resolve", args={"sku": "{row[SKU]}"}, var="m"),
        ])],
    )
    out = insert_loop_entry_arrivals(prog)
    assert len(out.statements[0].body) == 1 and isinstance(out.statements[0].body[0], Call)
    assert out.functions[0].body[0].kind == "navigation"  # the function body got it


def test_precondition_filter_first_step_still_gets_arrival():
    # Live 185 regression: the decomposer folded "回到列表页" into the search step AND marked it
    # precondition=True. precondition must NOT suppress the arrival — a precondition *filter* still
    # acts on the list and still needs to re-enter it on iteration 2+ (a pure-arrival precondition
    # would be kind='navigation', already excluded by the kind check, so this branch only ever sees
    # a precondition filter/action). The body must still gain the deterministic arrival.
    fn = FunctionDef(name="resolve", params=["sku"], returns=["material"], body=[
        Compute(var="base", expr="sku.rsplit('-',2)[0]"),
        Run(kind="filter", precondition=True,
            name="回到 Catalog > Products 列表页，清空 keyword，输入 {base} 并提交搜索",
            success_condition="出现 SKU={base} Configurable 父产品行"),
        Run(kind="navigation", name="开 Edit 编辑页", success_condition="进入编辑页"),
    ])
    p = insert_loop_entry_arrivals(Program(goal="g", functions=[fn], statements=[]))
    body = p.functions[0].body
    assert body[0].kind == "navigation" and body[0].success_condition == _ENTRY_ARRIVAL_SC
    assert isinstance(body[1], Compute)
    assert body[2].precondition is True  # original precondition filter preserved, just after the arrival


def test_idempotent():
    once = insert_loop_entry_arrivals(Program(goal="g", functions=[_resolve_fn()], statements=[]))
    twice = insert_loop_entry_arrivals(once)
    assert once.model_dump() == twice.model_dump()


def test_to_program_chains_from_state_through_inserted_arrival():
    # End to end: after insert + chain, the filter's FROM is the arrival's TO (on the list page) —
    # NOT empty — so the search step knows it's standing on the list.
    from gui_agent.core.orchestrator.decomposer import _FunctionDraft, _PlanDraft, _StepDraft, to_program

    draft = _PlanDraft(
        goal="g",
        steps=[_StepDraft(op="call", func="resolve", call_args={"sku": "WS08-XS-Blue"}, var="m")],
        functions=[_FunctionDraft(name="resolve", params=["sku"], returns=["material"], body=[
            _StepDraft(op="compute", var="base", expr="sku.rsplit('-',2)[0]"),
            _StepDraft(op="run", run_kind="filter", name="搜 {base}", success_condition="出现 SKU={base} 行"),
            _StepDraft(op="run", run_kind="navigation", name="开 Edit", success_condition="进入编辑页"),
        ])],
    )
    body = to_program(draft, "g").functions[0].body
    assert body[0].kind == "navigation"          # arrival inserted
    assert body[0].from_state == ""              # function-body entry
    # the filter (now body[2]) sees FROM = the arrival's list-page SC
    assert body[2].name == "搜 {base}"
    assert body[2].from_state == _ENTRY_ARRIVAL_SC
