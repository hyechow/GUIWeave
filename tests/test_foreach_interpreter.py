"""The DSL `foreach` loop: iterate a runtime-discovered collection (rows from collect_fn or legacy over=),
run a body per row binding {var[field]}, and AUTO-accumulate each iteration into a materialized table a
later data_query can query. This pins the interpreter semantics (the general iteration primitive).

Drives the interpreter synchronously with a mock executor — the same generator the live agent_loop
drives, so body Runs being yielded one-per-row is exactly the production path."""

from gui_agent.core.orchestrator import (
    Compute,
    Cond,
    Finish,
    ForEach,
    If,
    Interpreter,
    Program,
    ProgramRunner,
    Run,
    StatementOutcome,
    drive,
)
from gui_agent.core.orchestrator.program import Query, Read


def test_compute_bool_scalar_condition_compares_lowercase_true():
    program = Program(statements=[
        Compute(var="ok", expr="1 == 1"),
        If(
            cond=Cond(var="ok", field="", cmp="==", value="true"),
            then=[Finish(message="then")],
            otherwise=[Finish(message="else")],
        ),
    ])

    interp = Interpreter(program)
    reply = drive(interp, lambda run: StatementOutcome.completed(""))
    assert reply == "then"


def _review_program() -> Program:
    return Program(
        goal="找出 rating<=3 的昵称",
        statements=[
            Read(var="r", name="读取候选 review 行",  returns=["id"]),
            ForEach(
                var="row", over="r", into="reviews",
                body=[
                    Run(name="打开 review {row[id]} 详情", kind="navigation"),
                    Read(var="d", name="读取评分与昵称",  returns=["rating", "nickname"]),
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

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":
            return StatementOutcome.completed("", rows=[{"id": "347"}, {"id": "349"}, {"id": "351"}])
        if run.name.startswith("打开 review"):
            # {row[id]} was filled by the interpreter before the engine saw the Run
            seen_detail_targets.append(run.name)
            return StatementOutcome.completed("")
        if run.kind == "read":
            # which row are we on? the most recent detail-open target carries the id
            rid = seen_detail_targets[-1].split("review ", 1)[1].split(" ", 1)[0]
            return StatementOutcome.completed("", reads=details[rid])
        return StatementOutcome.completed("")

    interp = Interpreter(program)
    reply = drive(interp, execute)
    assert reply == "done"

    # the loop var was filled into each detail-open statement (one per row, in order)
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
            Read(var="r", name="读取候选行",  returns=["id"]),
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

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":
            return StatementOutcome.completed("", rows=[{"id": "1"}, {"id": "2"}])
        rid = run.name.split()[1]
        return StatementOutcome.completed("", reads={"rating": ratings[rid]})

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
            Read(var="r", name="读取候选行",  returns=["id"]),
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
    run = gen.send(StatementOutcome.completed("", rows=[{"id": "351"}, {"id": "347"}]))
    assert run.name == "打开评论 351 的详情"
    assert "351" in run.success_condition
    run = gen.send(StatementOutcome.completed(""))
    assert run.name == "打开评论 347 的详情"
    assert "347" in run.success_condition


def test_row_source_is_not_exposed_as_data_query_table():
    """Reproduces 20260622_214841: the source read row has an empty `rating` (the list has no rating
    column); the foreach drills each detail to fill rating. Only the accumulated `into` table — with
    the real ratings — may be a data_query source; the empty-rating source must not leak."""
    program = Program(
        goal="g",
        statements=[
            Read(var="r", name="读候选行", 
                returns=["id", "nickname", "rating"]),
            ForEach(var="row", over="r", into="reviews", body=[
                Run(name="打开 {row[id]}", kind="navigation"),
                Read(var="d", name="读详情评分",  returns=["rating", "nickname"]),
            ]),
        ],
    )
    det = {"1": {"rating": "5", "nickname": "Jane"}, "2": {"rating": "2", "nickname": "Emma"}}
    last: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":  # list shows id+nickname but NO rating (empty)
            return StatementOutcome.completed("", rows=[{"id": "1", "nickname": "", "rating": ""},
                                                   {"id": "2", "nickname": "", "rating": ""}])
        if run.name.startswith("打开"):
            last.append(run.name.split()[1])
            return StatementOutcome.completed("")
        return StatementOutcome.completed("", reads=det[last[-1]])

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
            Read(var="r", name="读取候选行",  returns=["id"]),
            ForEach(var="row", over="r", into="reviews",
                    body=[Run(name="打开 {row[id]}", kind="navigation")]),
            Finish(message="done"),
        ],
    )

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":
            return StatementOutcome.completed("", rows=[])   # nothing discovered
        return StatementOutcome.completed("")

    interp = Interpreter(program)
    drive(interp, execute)
    assert interp.env["reviews"].rows == []             # present + complete, just empty
    assert interp.env["reviews"].is_completed


def test_into_table_is_ready_when_data_query_is_yielded():
    """Timing regression 20260622_215814: the into table is populated when the LAST body read's result
    is sent — so by the time the interpreter yields the following data_query, materialized_tables()
    must already contain it. (The live data_query reads the provider fresh at that point, not a stale
    snapshot taken before the foreach finished.)"""
    program = Program(
        goal="g",
        statements=[
            Read(var="r", name="行",  returns=["id"]),
            ForEach(var="row", over="r", into="reviews", body=[
                Read(var="d", name="读 {row[id]}",  returns=["rating"]),
            ]),
            Query(var="q", name="筛",  returns=["rating"],
                sql="SELECT rating FROM reviews"),
        ],
    )
    interp = Interpreter(program)
    gen = interp.steps()
    run = next(gen)                                   # the read step (row source)
    run = gen.send(StatementOutcome.completed("", rows=[{"id": "1"}, {"id": "2"}]))
    # now driving foreach body reads; the into table must NOT exist until the last body read is sent
    while run.kind == "read" and not run.name.startswith("筛"):
        assert interp.materialized_tables() == []    # not yet — foreach still iterating
        run = gen.send(StatementOutcome.completed("", reads={"rating": "3"}))
    # the loop exited because the yielded run is now the data_query — and the into table is READY
    assert run.kind == "data_query"
    assert [m["caption"] for m in interp.materialized_tables()] == ["reviews"]
    assert len(interp.materialized_tables()[0]["rows"]) == 2


def test_foreach_into_defaults_to_var_plural():
    program = Program(
        goal="g",
        statements=[
            Read(var="r", name="行",  returns=["id"]),
            ForEach(var="row", over="r",  # no `into` → defaults to f"{var}s"
                    body=[Read(var="d", name="读 {row[id]}",  returns=["v"])]),
        ],
    )

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":
            return StatementOutcome.completed("", rows=[{"id": "1"}])
        return StatementOutcome.completed("", reads={"v": "x"})

    runner = ProgramRunner(execute)
    result = runner.run(program)
    assert "rows" in result.env  # default into = "row" + "s"
    assert result.env["rows"].rows == [{"id": "1", "v": "x"}]


def test_foreach_fails_honestly_when_declared_column_missing_from_all_rows():
    """Column-completeness safety net (WebArena task 63 regression). When a declared returns
    column is absent (no key) from EVERY collected row — the grid silently dropped it — the
    foreach must NOT silently accumulate a key column's worth of nothing. It publishes an empty
    into-table and marks the run finish_incomplete so a downstream data_query can't manufacture
    a confident wrong answer from missing data."""
    program = Program(
        goal="按 Customer Email 统计订单数",
        statements=[
            Read(var="r", name="读取订单行", 
                returns=["ID", "Customer Email", "Status"]),
            ForEach(var="row", over="r", into="orders",
                    returns=["ID", "Customer Email", "Status"],
                    body=[Run(name="noop {row[ID]}", kind="navigation")]),
            Finish(message="done"),
        ],
    )

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":
            # The grid rendered only ID + Status — Customer Email key never created.
            return StatementOutcome.completed("", rows=[
                {"ID": "1", "Status": "Complete"},
                {"ID": "2", "Status": "Complete"},
            ])
        return StatementOutcome.completed("")

    interp = Interpreter(program)
    drive(interp, execute)
    assert interp.finish_incomplete is True
    assert interp.env["orders"].rows == []
    assert not interp.env["orders"].is_completed


def test_foreach_does_not_misfire_on_present_but_blank_column():
    """The safety net keys on 'absent as a key', not 'blank value' — a rendered-but-empty cell
    (legitimate: e.g. an optional column blank for this filtered set) keeps its key and must NOT
    trip the guard."""
    program = Program(
        goal="g",
        statements=[
            Read(var="r", name="读取行",  returns=["ID", "Coupon"]),
            ForEach(var="row", over="r", into="orders", returns=["ID", "Coupon"],
                    body=[Run(name="noop {row[ID]}", kind="navigation")]),
            Finish(message="done"),
        ],
    )

    def execute(run: Run) -> StatementOutcome:
        if run.var == "r" and run.kind == "read":
            return StatementOutcome.completed("", rows=[
                {"ID": "1", "Coupon": ""},   # Coupon present as a key, just blank
                {"ID": "2", "Coupon": ""},
            ])
        return StatementOutcome.completed("")

    interp = Interpreter(program)
    drive(interp, execute)
    assert interp.finish_incomplete is False
    assert interp.env["orders"].rows == [
        {"ID": "1", "Coupon": ""}, {"ID": "2", "Coupon": ""},
    ]


def test_foreach_does_not_turn_incomplete_collection_into_empty_complete_table():
    program = Program(statements=[
        ForEach(var="row", into="candidates", row_fields=["name"], body=[]),
        Finish(message="done"),
    ])
    interp = Interpreter(program, collect_fn=lambda *_args, **_kwargs: None)

    drive(interp, lambda run: StatementOutcome.completed(""))

    assert interp.finish_incomplete is True
    assert not interp.env["candidates"].is_completed
    assert interp.env["candidates"].rows == []
