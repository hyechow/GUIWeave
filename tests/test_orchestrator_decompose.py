"""Program Decomposer (#2): goal -> DSL Program.

No LLM here — the gate is deterministic. We test the two pieces the LLM call is built
on: the flat-draft -> Program AST converter (to_program) and the shape validator
(validate_program), plus an end-to-end that the converted program drives correctly
through the interpreter. The live LLM decompose is exercised by tmp_scripts, not the gate.
"""

from __future__ import annotations

from gui_agent.core.orchestrator import (
    Cond,
    Finish,
    If,
    Program,
    ProgramRunner,
    Run,
    RunResult,
    to_program,
    validate_program,
)
from gui_agent.core.orchestrator.decomposer import _PlanDraft, _StepDraft, _table_schema_prompt


def _connectivity_draft() -> _PlanDraft:
    return _PlanDraft(
        reasoning="进页→填起终点触发检测→读连通→分支",
        goal="检测连通；连通建单否则报原因",
        steps=[
            _StepDraft(op="run", run_kind="navigation", name="进入连通性工具页",
                       success_condition="显示起终点输入框"),
            _StepDraft(op="run", run_kind="action", name="填起终点并检测",
                       success_condition="检测结果已显示"),
            _StepDraft(op="run", run_kind="read", var="d", name="读连通判定",
                       returns=["连通判定", "不可达原因"],
                       read_spec="连通判定：看起终点间图标，绿✓=连通；不可达原因：连通时空"),
            _StepDraft(op="if", cond_var="d", cond_field="连通判定", cond_cmp="==", cond_value="连通",
                       then=[_StepDraft(op="run", run_kind="action", name="建单",
                                        success_condition="建单成功")],
                       otherwise=[_StepDraft(op="finish", message="不可达：{d[不可达原因]}")]),
        ],
    )


def test_to_program_maps_runs_kinds_and_var():
    prog = to_program(_connectivity_draft(), "fallback goal")
    assert prog.goal == "检测连通；连通建单否则报原因"
    nav, act, rd, branch = prog.statements
    assert isinstance(nav, Run) and nav.kind == "navigation" and nav.var is None  # 空 var → None
    assert isinstance(act, Run) and act.kind == "action"
    assert isinstance(rd, Run) and rd.kind == "read" and rd.var == "d" and rd.returns == ["连通判定", "不可达原因"]
    assert "绿✓=连通" in rd.read_spec          # 任务级读取说明从 draft 透传到 read Run
    assert isinstance(branch, If)


def test_to_program_builds_if_and_finish():
    prog = to_program(_connectivity_draft(), "")
    branch = prog.statements[-1]
    assert isinstance(branch, If)
    assert branch.cond == Cond(var="d", field="连通判定", cmp="==", value="连通")
    assert isinstance(branch.then[0], Run) and branch.then[0].name == "建单"
    assert isinstance(branch.otherwise[0], Finish)
    assert branch.otherwise[0].message == "不可达：{d[不可达原因]}"


def test_to_program_maps_extended_condition_values():
    draft = _PlanDraft(steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读订单状态", returns=["状态"]),
        _StepDraft(
            op="if", cond_var="r", cond_field="状态", cond_cmp="in",
            cond_values=["待执行", "进行中"],
            then=[_StepDraft(op="finish", message="ok")],
            otherwise=[_StepDraft(op="finish", message="bad")],
        ),
    ])
    branch = to_program(draft, "").statements[-1]
    assert isinstance(branch, If)
    assert branch.cond == Cond(var="r", field="状态", cmp="in", values=["待执行", "进行中"])


def test_to_program_maps_data_query_sql():
    draft = _PlanDraft(steps=[
        _StepDraft(
            op="run",
            run_kind="data_query",
            var="q",
            name="统计订单邮箱",
            returns=["emails"],
            sql="SELECT customer_email FROM data GROUP BY customer_email HAVING COUNT(*) = 2",
            data_scope="current",
        ),
        _StepDraft(op="finish", message="{q[emails]}"),
    ])
    prog = to_program(draft, "")
    query = prog.statements[0]
    assert isinstance(query, Run)
    assert query.kind == "data_query"
    assert query.var == "q"
    assert query.returns == ["emails"]
    assert "GROUP BY" in query.sql
    assert query.data_scope == "current"
    assert validate_program(prog) == []


def test_table_schema_prompt_omits_row_values_and_shows_sql_names():
    hint = _table_schema_prompt(
        [
            {
                "index": 1,
                "caption": "Top Search Terms",
                "headers": ["Search Term", "Results", "Uses"],
                "rows": [
                    {"Search Term": "hollister", "Results": "1", "Uses": "19"},
                ],
                "row_count": 1,
                "partial": False,
            }
        ]
    )

    assert "top_search_terms" in hint
    assert "Search Term->search_term" in hint
    assert "hollister" not in hint
    assert "19" not in hint


def test_to_program_goal_falls_back_when_draft_goal_empty():
    draft = _PlanDraft(goal="", steps=[_StepDraft(op="run", name="x")])
    assert to_program(draft, "原始目标").goal == "原始目标"


def test_to_program_unknown_kind_defaults_action():
    draft = _PlanDraft(steps=[_StepDraft(op="run", run_kind="garbage", name="x")])
    assert to_program(draft, "").statements[0].kind == "action"


def test_validate_clean_program_has_no_issues():
    assert validate_program(to_program(_connectivity_draft(), "")) == []


def test_validate_empty_program():
    assert validate_program(Program(statements=[])) == ["程序为空：至少要有一个 run 步骤"]


def test_validate_if_references_unknown_var():
    prog = Program(statements=[
        Run(var="d", name="读", kind="read", returns=["连通判定"]),
        If(cond=Cond(var="X", field="连通判定", value="连通"),
           then=[Finish(message="a")], otherwise=[Finish(message="b")]),
    ])
    issues = validate_program(prog)
    # 路径敏感：未知/未在路径上产生的 cond var 统一报「尚未产生」（含 X 根本不是任何 read 的 var 这种）
    assert any("尚未产生" in i for i in issues)


def test_validate_if_references_unknown_field():
    prog = Program(statements=[
        Run(var="d", name="读", kind="read", returns=["连通判定"]),
        If(cond=Cond(var="d", field="别的字段", value="x"),
           then=[Finish(message="a")], otherwise=[Finish(message="b")]),
    ])
    issues = validate_program(prog)
    assert any("不在该 read/data_query 步返回的字段（returns）里" in i for i in issues)


def test_validate_condition_operator_operands():
    missing_values = Program(statements=[
        Run(var="r", name="读", kind="read", returns=["状态"]),
        If(cond=Cond(var="r", field="状态", cmp="in"),
           then=[Finish(message="a")], otherwise=[Finish(message="b")]),
    ])
    assert any("缺少 cond_values" in i for i in validate_program(missing_values))

    missing_value = Program(statements=[
        Run(var="r", name="读", kind="read", returns=["提示"]),
        If(cond=Cond(var="r", field="提示", cmp="contains"),
           then=[Finish(message="a")], otherwise=[Finish(message="b")]),
    ])
    assert any("缺少 cond_value" in i for i in validate_program(missing_value))


def test_validate_read_without_returns_or_var():
    no_returns = Program(statements=[Run(var="d", name="读", kind="read", returns=[])])
    assert any("没有 returns 字段" in i for i in validate_program(no_returns))
    no_var = Program(statements=[Run(name="读", kind="read", returns=["x"])])
    assert any("没有绑定 var" in i for i in validate_program(no_var))


def test_validate_data_query_requires_var_returns_and_sql():
    no_returns = Program(statements=[Run(var="q", name="查", kind="data_query", sql="SELECT 1")])
    assert any("data_query" in i and "returns" in i for i in validate_program(no_returns))
    no_var = Program(statements=[Run(name="查", kind="data_query", returns=["x"], sql="SELECT 1")])
    assert any("data_query" in i and "绑定 var" in i for i in validate_program(no_var))
    no_sql = Program(statements=[Run(var="q", name="查", kind="data_query", returns=["x"])])
    assert any("data_query" in i and "没有 sql" in i for i in validate_program(no_sql))


def test_validate_walks_into_branches():
    # read 嵌在 then 分支里也能被识别为 cond 来源
    prog = Program(statements=[
        Run(var="top", name="读顶层", kind="read", returns=["flag"]),
        If(cond=Cond(var="top", field="flag", value="1"),
           then=[
               Run(var="inner", name="读分支内", kind="read", returns=["sub"]),
               If(cond=Cond(var="inner", field="sub", value="ok"),
                  then=[Finish(message="ok")], otherwise=[Finish(message="no")]),
           ],
           otherwise=[Finish(message="x")]),
    ])
    assert validate_program(prog) == []


def test_decomposed_program_drives_correct_branch_end_to_end():
    prog = to_program(_connectivity_draft(), "")
    def _exec(run: Run) -> RunResult:
        reads = {"连通判定": "连通", "不可达原因": ""} if run.var == "d" else {}
        return RunResult(completed=True, reads=reads, summary=run.name)
    res = ProgramRunner(_exec).run(prog)
    assert any(r.name == "建单" for r in res.run_log)   # 连通 → 建单支
    assert res.failed is False
