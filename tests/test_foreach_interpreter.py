"""The DSL `foreach` loop: iterate a runtime-discovered collection (rows from collect_fn or legacy over=),
run a body per row binding {var[field]}, and AUTO-accumulate each iteration into a materialized table a
later data_query can query. This pins the interpreter semantics (the general iteration primitive).

Drives the interpreter synchronously with a mock executor — the same generator the live agent_loop
drives, so body Runs being yielded one-per-row is exactly the production path."""

from gui_agent.core.orchestrator import (
    Finish,
    ForEach,
    Interpreter,
    Program,
    ProgramRunner,
    Run,
    RunResult,
    drive,
)


def _review_program() -> Program:
    return Program(
        goal="找出 rating<=3 的昵称",
        statements=[
            Run(var="r", name="读取候选 review 行", kind="read", returns=["id"]),
            ForEach(
                var="row", over="r", into="reviews",
                body=[
                    Run(name="打开 review {row[id]} 详情", kind="navigation"),
                    Run(var="d", name="读取评分与昵称", kind="read", returns=["rating", "nickname"]),
                ],
            ),
            Finish(message="done"),
        ],
    )


def test_foreach_iterates_rows_and_accumulates_into_table():
    program = _review_program()
    # mock executor: the read step returns 3 rows; each detail read returns that row's rating/nickname.
    details = {
        "347": {"rating": "5", "nickname": "Jane"},
        "349": {"rating": "2", "nickname": "Emma"},
        "351": {"rating": "1", "nickname": "Seam"},
    }
    seen_detail_targets: list[str] = []

    def execute(run: Run) -> RunResult:
        if run.var == "r" and run.kind == "read":
            return RunResult(completed=True, rows=[{"id": "347"}, {"id": "349"}, {"id": "351"}])
        if run.name.startswith("打开 review"):
            # {row[id]} was filled by the interpreter before the engine saw the Run
            seen_detail_targets.append(run.name)
            return RunResult(completed=True)
        if run.kind == "read":
            # which row are we on? the most recent detail-open target carries the id
            rid = seen_detail_targets[-1].split("review ", 1)[1].split(" ", 1)[0]
            return RunResult(completed=True, reads=details[rid])
        return RunResult(completed=True)

    interp = Interpreter(program)
    reply = drive(interp, execute)
    assert reply == "done"

    # the loop var was filled into each detail-open milestone (one per row, in order)
    assert seen_detail_targets == [
        "打开 review 347 详情", "打开 review 349 详情", "打开 review 351 详情",
    ]
    # auto-accumulated table: row's own field (id) + body read fields (rating, nickname), one per row
    table = interp.env["reviews"]
    assert table.rows == [
        {"id": "347", "rating": "5", "nickname": "Jane"},
        {"id": "349", "rating": "2", "nickname": "Emma"},
        {"id": "351", "rating": "1", "nickname": "Seam"},
    ]
    # exposed as a data_query-shaped snapshot (caption=var, complete, headers unioned)
    mats = interp.materialized_tables()
    reviews = next(m for m in mats if m["caption"] == "reviews")
    assert reviews["partial"] is False
    assert set(reviews["headers"]) == {"id", "rating", "nickname"}
    assert len(reviews["rows"]) == 3
    # ONLY the foreach into table is exposed — NOT the source `r` (its raw rows carry no
    # detail fields and would pollute the data_query source; regression 20260622_214841).
    assert [m["caption"] for m in mats] == ["reviews"]


def test_foreach_accumulates_body_action_returns():
    program = Program(
        goal="g",
        statements=[
            Run(var="r", name="读取候选行", kind="read", returns=["id"]),
            ForEach(var="row", over="r", into="details", body=[
                Run(
                    var="d",
                    name="打开 {row[id]} 详情",
                    kind="navigation",
                    returns=["rating"],
                    read_spec="rating：详情页评分。",
                ),
            ]),
        ],
    )
    ratings = {"1": "5", "2": "2"}

    def execute(run: Run) -> RunResult:
        if run.var == "r" and run.kind == "read":
            return RunResult(completed=True, rows=[{"id": "1"}, {"id": "2"}])
        rid = run.name.split()[1]
        return RunResult(completed=True, reads={"rating": ratings[rid]})

    interp = Interpreter(program)
    drive(interp, execute)

    assert interp.env["details"].rows == [
        {"id": "1", "rating": "5"},
        {"id": "2", "rating": "2"},
    ]


def test_foreach_target_identity_is_added_to_success_condition():
    program = Program(
        goal="g",
        statements=[
            Run(var="r", name="读取候选行", kind="read", returns=["id"]),
            ForEach(var="row", over="r", into="details", body=[
                Run(
                    name="打开评论 {row[id]} 的详情",
                    kind="navigation",
                    success_condition="进入该评论详情页，显示评分与昵称",
                ),
            ]),
        ],
    )
    interp = Interpreter(program)
    gen = interp.steps()
    run = next(gen)
    run = gen.send(RunResult(completed=True, rows=[{"id": "351"}, {"id": "347"}]))
    assert run.name == "打开评论 351 的详情"
    assert "351" in run.success_condition
    run = gen.send(RunResult(completed=True))
    assert run.name == "打开评论 347 的详情"
    assert "347" in run.success_condition


def test_row_source_is_not_exposed_as_data_query_table():
    """Reproduces 20260622_214841: the source read row has an empty `rating` (the list has no rating
    column); the foreach drills each detail to fill rating. Only the accumulated `into` table — with
    the real ratings — may be a data_query source; the empty-rating source must not leak."""
    program = Program(
        goal="g",
        statements=[
            Run(var="r", name="读候选行", kind="read",
                returns=["id", "nickname", "rating"]),
            ForEach(var="row", over="r", into="reviews", body=[
                Run(name="打开 {row[id]}", kind="navigation"),
                Run(var="d", name="读详情评分", kind="read", returns=["rating", "nickname"]),
            ]),
        ],
    )
    det = {"1": {"rating": "5", "nickname": "Jane"}, "2": {"rating": "2", "nickname": "Emma"}}
    last: list[str] = []

    def execute(run: Run) -> RunResult:
        if run.var == "r" and run.kind == "read":  # list shows id+nickname but NO rating (empty)
            return RunResult(completed=True, rows=[{"id": "1", "nickname": "", "rating": ""},
                                                   {"id": "2", "nickname": "", "rating": ""}])
        if run.name.startswith("打开"):
            last.append(run.name.split()[1])
            return RunResult(completed=True)
        return RunResult(completed=True, reads=det[last[-1]])

    interp = Interpreter(program)
    drive(interp, execute)
    mats = interp.materialized_tables()
    assert [m["caption"] for m in mats] == ["reviews"]          # `r` (empty rating) NOT exposed
    rows = mats[0]["rows"]
    assert {r["rating"] for r in rows} == {"5", "2"}            # detail ratings, not the empty list ones


def test_foreach_empty_collection_publishes_empty_table():
    program = Program(
        goal="g",
        statements=[
            Run(var="r", name="读取候选行", kind="read", returns=["id"]),
            ForEach(var="row", over="r", into="reviews",
                    body=[Run(name="打开 {row[id]}", kind="navigation")]),
            Finish(message="done"),
        ],
    )

    def execute(run: Run) -> RunResult:
        if run.var == "r" and run.kind == "read":
            return RunResult(completed=True, rows=[])   # nothing discovered
        return RunResult(completed=True)

    interp = Interpreter(program)
    drive(interp, execute)
    assert interp.env["reviews"].rows == []             # present + complete, just empty
    assert interp.env["reviews"].completed is True


def test_into_table_is_ready_when_data_query_is_yielded():
    """Timing regression 20260622_215814: the into table is populated when the LAST body read's result
    is sent — so by the time the interpreter yields the following data_query, materialized_tables()
    must already contain it. (The live data_query reads the provider fresh at that point, not a stale
    snapshot taken before the foreach finished.)"""
    program = Program(
        goal="g",
        statements=[
            Run(var="r", name="行", kind="read", returns=["id"]),
            ForEach(var="row", over="r", into="reviews", body=[
                Run(var="d", name="读 {row[id]}", kind="read", returns=["rating"]),
            ]),
            Run(var="q", name="筛", kind="data_query", returns=["rating"],
                sql="SELECT rating FROM reviews"),
        ],
    )
    interp = Interpreter(program)
    gen = interp.steps()
    run = next(gen)                                   # the read step (row source)
    run = gen.send(RunResult(completed=True, rows=[{"id": "1"}, {"id": "2"}]))
    # now driving foreach body reads; the into table must NOT exist until the last body read is sent
    while run.kind == "read" and not run.name.startswith("筛"):
        assert interp.materialized_tables() == []    # not yet — foreach still iterating
        run = gen.send(RunResult(completed=True, reads={"rating": "3"}))
    # the loop exited because the yielded run is now the data_query — and the into table is READY
    assert run.kind == "data_query"
    assert [m["caption"] for m in interp.materialized_tables()] == ["reviews"]
    assert len(interp.materialized_tables()[0]["rows"]) == 2


def test_foreach_into_defaults_to_var_plural():
    program = Program(
        goal="g",
        statements=[
            Run(var="r", name="行", kind="read", returns=["id"]),
            ForEach(var="row", over="r",  # no `into` → defaults to f"{var}s"
                    body=[Run(var="d", name="读 {row[id]}", kind="read", returns=["v"])]),
        ],
    )

    def execute(run: Run) -> RunResult:
        if run.var == "r" and run.kind == "read":
            return RunResult(completed=True, rows=[{"id": "1"}])
        return RunResult(completed=True, reads={"v": "x"})

    runner = ProgramRunner(execute)
    result = runner.run(program)
    assert "rows" in result.env  # default into = "row" + "s"
    assert result.env["rows"].rows == [{"id": "1", "v": "x"}]
