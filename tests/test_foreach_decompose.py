"""Decomposer + data_query wiring for the `foreach` general-iteration primitive:
- draft (op="foreach" / list_read) → AST round-trips,
- validate_program enforces over=a list_read and an in-scope {loop_var[field]} body,
- a foreach's materialized `into` table is queryable by a following data_query (the whole point:
  collect per-item detail, then filter/aggregate the set)."""

from gui_agent.core.orchestrator import ForEach, Interpreter, Program, Run, RunResult, drive
from gui_agent.core.orchestrator.data_query import execute_data_query
from gui_agent.core.orchestrator.decomposer import _PlanDraft, _StepDraft, to_program, validate_program


def _good_draft() -> _PlanDraft:
    return _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读候选行", returns=["id"], list_read=True),
        _StepDraft(op="foreach", loop_var="row", over="r", into="reviews", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开 review {row[id]} 详情"),
            _StepDraft(op="run", run_kind="read", var="d", name="读评分昵称", returns=["rating", "nickname"]),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛 rating<=3", returns=["nickname"],
                   sql="SELECT nickname FROM reviews WHERE CAST(rating AS INTEGER) <= 3", data_scope="current"),
        _StepDraft(op="finish", message="{q[nickname]}"),
    ])


def test_foreach_draft_round_trips_and_validates_clean():
    program = to_program(_good_draft(), "g")
    assert [type(s).__name__ for s in program.statements] == ["Run", "ForEach", "Run", "Finish"]
    fe = program.statements[1]
    assert isinstance(fe, ForEach)
    assert (fe.var, fe.over, fe.into) == ("row", "r", "reviews")
    assert [type(b).__name__ for b in fe.body] == ["Run", "Run"]
    assert program.statements[0].list_read is True
    assert validate_program(program) == []  # {row[id]} in-scope; over is a list_read


def test_orphan_list_read_used_directly_is_scalarized():
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(
            op="run",
            run_kind="read",
            var="winners",
            name="读取获奖者姓名",
            returns=["winner_names"],
            list_read=True,
            read_spec="winner_names: 从搜索结果中提取所有获奖者姓名，每行一个对象",
        ),
        _StepDraft(op="finish", message="{winners[winner_names]}"),
    ])

    program = to_program(draft, "g")
    read = program.statements[0]

    assert isinstance(read, Run)
    assert read.list_read is False
    assert validate_program(program) == []


def test_validate_rejects_over_that_is_not_a_list_read():
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读", returns=["id"]),  # NOT list_read
        _StepDraft(op="foreach", loop_var="row", over="r",
                   body=[_StepDraft(op="run", run_kind="read", var="d", name="读 {row[id]}", returns=["v"])]),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("不是一个 list_read" in i for i in issues)


def test_validate_rejects_body_ref_to_unbound_var():
    # {bad[x]} in the body isn't the loop var nor a prior read → caught.
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读", returns=["id"], list_read=True),
        _StepDraft(op="foreach", loop_var="row", over="r",
                   body=[_StepDraft(op="run", run_kind="navigation", name="打开 {bad[x]}")]),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("bad" in i for i in issues)


def test_validate_rejects_list_read_direct_query_missing_detail_fields():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"], list_read=True),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM data WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("不要跳过 foreach" in i for i in issues)


def test_validate_infers_row_read_spec_when_list_read_flag_missing():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"], read_spec="id：逐行读取列表中每条记录的 ID，每行一个对象。"),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM data WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("不要跳过 foreach" in i for i in issues)


def test_validate_rejects_foreach_table_query_missing_body_fields():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"], list_read=True),
        _StepDraft(op="foreach", loop_var="row", over="r", into="detail_rows", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开记录 {row[id]} 的详情"),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM detail_rows WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("foreach body 没有通过 returns 产出的字段" in i for i in issues)


def test_validate_rejects_post_foreach_query_missing_body_fields_without_table_ref():
    draft = _PlanDraft(goal="返回详情分数<=3的负责人", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读取候选行 id",
                   returns=["id"], list_read=True),
        _StepDraft(op="foreach", loop_var="row", over="r", into="detail_rows", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开记录 {row[id]} 的详情"),
        ]),
        _StepDraft(op="run", run_kind="data_query", var="q", name="筛详情分数<=3",
                   returns=["owner_name"],
                   sql="SELECT owner_name FROM data WHERE CAST(detail_score AS INTEGER) <= 3"),
    ])
    issues = validate_program(to_program(draft, "g"))
    assert any("位于 foreach 之后" in i for i in issues)


def test_nested_foreach_in_body_is_dropped():
    # One level only: a nested foreach in the body is stripped at conversion (deterministic backstop).
    draft = _PlanDraft(goal="g", steps=[
        _StepDraft(op="run", run_kind="read", var="r", name="读", returns=["id"], list_read=True),
        _StepDraft(op="foreach", loop_var="row", over="r", body=[
            _StepDraft(op="run", run_kind="navigation", name="打开 {row[id]}"),
            _StepDraft(op="foreach", loop_var="inner", over="r", body=[]),  # nested → dropped
        ]),
    ])
    program = to_program(draft, "g")
    fe = program.statements[1]
    assert all(type(b).__name__ != "ForEach" for b in fe.body)


def test_data_query_runs_on_foreach_materialized_table():
    """End-to-end of the accumulate→query seam (deterministic): run the program's foreach with a mock
    executor, then run the data_query SQL against the interpreter's materialized table."""
    program = to_program(_good_draft(), "g")
    details = {"1": {"rating": "5", "nickname": "Jane"},
               "2": {"rating": "2", "nickname": "Emma"},
               "3": {"rating": "3", "nickname": "Seam"}}
    last_open: list[str] = []

    def execute(run: Run) -> RunResult:
        if run.list_read:
            return RunResult(completed=True, rows=[{"id": "1"}, {"id": "2"}, {"id": "3"}])
        if run.name.startswith("打开"):
            last_open.append(run.name.split("review ", 1)[1].split(" ", 1)[0])
            return RunResult(completed=True)
        if run.kind == "read":
            return RunResult(completed=True, reads=details[last_open[-1]])
        if run.kind == "data_query":
            return RunResult(completed=True)  # the live loop runs SQL; here we assert it separately
        return RunResult(completed=True)

    interp = Interpreter(program)
    drive(interp, execute)
    snapshots = interp.materialized_tables()  # what the live data_query path folds into its source
    out = execute_data_query(
        snapshots, "SELECT nickname FROM reviews WHERE CAST(rating AS INTEGER) <= 3", ["nickname"],
        require_complete=False,
    )
    # rating<=3 → Emma(2) + Seam(3), not Jane(5)
    assert "Emma" in str(out) and "Seam" in str(out) and "Jane" not in str(out)
