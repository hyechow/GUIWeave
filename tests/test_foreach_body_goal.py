"""Agentic per-row sub-goal (`ForEach.body_goal`): instead of pre-baked body Stmts, each row's
sub-goal is decomposed fresh at runtime and its Runs driven as full milestones (yield-from), with
the sub-program's produced fields merged back into the row. This is the general mechanism for
per-row tasks too complex for a fixed step list — e.g. WebArena 185 (child variant → search parent
configurable → read primary Material), the representative per-row entity-join class.

Driven synchronously with mock executor + mock subdecompose_fn (no LLM/live), exercising the exact
generator the live agent_loop drives.
"""

from gui_agent.core.orchestrator import (
    Compute,
    Finish,
    ForEach,
    Interpreter,
    Program,
    Query,
    Run,
    RunResult,
    drive,
)
from gui_agent.core.orchestrator.validator import validate_program
from gui_agent.core.orchestrator.program import Read


_MATERIAL_OF = {"Minerva": "Cotton", "Eos": "Fleece"}


def _body_goal_program() -> Program:
    return Program(
        goal="material of products with 3 units left",
        statements=[
            ForEach(
                var="row",
                into="materials",
                returns=["material"],  # per-row CONTRACT (produced by the sub-goal, not collected)
                body_goal="找到 {row[Name]} 对应的父配置型产品，读出其主材质 → 返回 material",
            ),
            Finish(message="materials: {q[result]}"),
        ],
    )


def test_body_goal_decomposes_per_row_and_merges_contract():
    program = _body_goal_program()
    # collect_fn gathers the variant rows (collect columns = the fields the sub-goal templates → Name)
    collect_calls: list[list] = []

    def collect_fn(target, returns, limit=None):
        collect_calls.append(list(returns))
        return [{"Name": "Minerva LumaTech V-Tee-XS-Blue"}, {"Name": "Eos V-Neck Hoodie-S-Blue"}]

    # subdecompose_fn: the rendered goal carries the concrete variant name; emit a sub-program that
    # reads material. The goal text is embedded in the run name so the mock executor can route.
    sub_goals_seen: list[str] = []

    def subdecompose_fn(goal: str) -> Program:
        sub_goals_seen.append(goal)
        return Program(statements=[
            Read(var="d", name=f"读父产品主材质::{goal}",  returns=["material"]),
        ])

    def execute(run: Run) -> RunResult:
        if run.kind == "read" and "读父产品主材质" in run.name:
            brand = "Minerva" if "Minerva" in run.name else "Eos"
            return RunResult(completed=True, reads={"material": _MATERIAL_OF[brand]})
        return RunResult(completed=True)

    interp = Interpreter(program, collect_fn=collect_fn, subdecompose_fn=subdecompose_fn)
    drive(interp, execute)

    # collect columns = the row fields the sub-goal templates (Name), NOT the contract (material)
    assert collect_calls == [["Name"]]
    # the sub-goal was decomposed once PER ROW, with the concrete variant name substituted in
    assert len(sub_goals_seen) == 2
    assert "Minerva LumaTech V-Tee-XS-Blue" in sub_goals_seen[0]
    assert "Eos V-Neck Hoodie-S-Blue" in sub_goals_seen[1]
    # the sub-program's produced `material` merged back into each row → queryable into-table
    table = interp.env["materials"]
    assert table.rows == [
        {"Name": "Minerva LumaTech V-Tee-XS-Blue", "material": "Cotton"},
        {"Name": "Eos V-Neck Hoodie-S-Blue", "material": "Fleece"},
    ]


def test_body_goal_explicit_row_and_output_fields_drive_contract():
    program = Program(
        goal="update selected rows and summarize",
        statements=[
            ForEach(
                var="row",
                into="price_updates",
                row_fields=["sku", "detail_url", "current_price"],
                output_fields=["sku", "old_price", "new_price", "status", "size"],
                body_goal=(
                    "判断 {row[sku]} 是否属于目标规格；若是打开 {row[detail_url]}，"
                    "读取旧价、计算新价、保存并返回 old_price/new_price/status/size"
                ),
            ),
            Query(
                var="q",
                name="汇总调价结果",
                returns=["result"],
                sql=(
                    "SELECT sku, old_price, new_price, status "
                    "FROM price_updates WHERE size = '28'"
                ),
            ),
            Finish(message="{q[result]}"),
        ],
    )

    codes = _codes(program)

    assert "FOREACH_BODY_GOAL_MISSING_RETURNS" not in codes
    assert "FOREACH_DQ_DETAIL_FIELD_MISSING" not in codes


def test_body_goal_legacy_returns_do_not_hide_missing_output_fields():
    program = Program(
        statements=[
            ForEach(
                var="row",
                into="sahara_size28_rows",
                returns=["sku", "detail_url", "current_price"],
                body_goal="判断 {row[sku]} 是否为目标规格；若是打开详情读价改价保存",
            ),
            Query(
                var="q",
                name="汇总调价结果",
                returns=["results"],
                sql=(
                    "SELECT sku, old_price, new_price, status "
                    "FROM sahara_size28_rows WHERE size = '28'"
                ),
            ),
        ],
    )

    assert "FOREACH_DQ_DETAIL_FIELD_MISSING" in _codes(program)


def test_explicit_body_output_fields_must_be_actually_produced():
    program = Program(
        statements=[
            ForEach(
                var="row",
                into="updates",
                row_fields=["sku", "price"],
                output_fields=["old_price", "new_price", "status"],
                body=[
                    Compute(var="new_price", expr="round(float(row['price']) * 0.865, 2)"),
                ],
            ),
            Query(
                var="q",
                name="汇总",
                returns=["result"],
                sql="SELECT sku, old_price, new_price, status FROM updates",
            ),
        ],
    )

    assert "FOREACH_DQ_DETAIL_FIELD_MISSING" in _codes(program)


def test_body_goal_output_fields_collect_compute_scalars():
    program = Program(
        goal="compute per row",
        statements=[
            ForEach(
                var="row",
                into="updates",
                row_fields=["sku", "price"],
                output_fields=["new_price"],
                body_goal="处理 {row[sku]}，按 {row[price]} 计算 new_price",
            ),
        ],
    )

    def collect_fn(target, returns, limit=None):
        assert list(returns) == ["sku", "price"]
        return [
            {"sku": "A-28", "price": "100"},
            {"sku": "B-28", "price": "80"},
        ]

    def subdecompose_fn(goal: str) -> Program:
        return Program(statements=[
            Compute(var="new_price", expr="round(float(row['price']) * 0.865, 2)"),
        ])

    interp = Interpreter(program, collect_fn=collect_fn, subdecompose_fn=subdecompose_fn)
    drive(interp, lambda run: RunResult(completed=True))

    assert interp.env["updates"].rows == [
        {"sku": "A-28", "price": "100", "new_price": "86.5"},
        {"sku": "B-28", "price": "80", "new_price": "69.2"},
    ]


def test_body_goal_query_must_filter_on_body_goal_outputs_not_row_guess():
    program = Program(
        statements=[
            ForEach(
                var="row",
                into="sahara_leggings_28_rows",
                row_fields=["sku", "name", "type", "action_url"],
                output_fields=["sku", "old_price", "new_price"],
                body_goal=(
                    "判断 {row[sku]} 是否为 size 28 的 Sahara leggings 变体；若是，"
                    "打开 {row[action_url]} 读当前价→计算新价→更新并保存→返回 old_price/new_price"
                ),
            ),
            Query(
                var="q",
                name="确认所有 size 28 Sahara leggings 变体的价格更新结果",
                returns=["result"],
                sql=(
                    "SELECT sku, old_price, new_price FROM sahara_leggings_28_rows "
                    "WHERE sku LIKE '%Sahara%' AND sku LIKE '%28%'"
                ),
            ),
        ],
    )

    assert "FOREACH_BODY_GOAL_QUERY_ROW_PREDICATE" in _codes(program)


def test_body_goal_query_can_filter_on_nonempty_body_goal_outputs():
    program = Program(
        statements=[
            ForEach(
                var="row",
                into="sahara_leggings_28_rows",
                row_fields=["sku", "name", "type", "action_url"],
                output_fields=["sku", "old_price", "new_price"],
                body_goal=(
                    "判断 {row[sku]} 是否为 size 28 的 Sahara leggings 变体；若是，"
                    "打开 {row[action_url]} 读当前价→计算新价→更新并保存→返回 old_price/new_price"
                ),
            ),
            Query(
                var="q",
                name="确认所有 size 28 Sahara leggings 变体的价格更新结果",
                returns=["result"],
                sql=(
                    "SELECT sku, old_price, new_price FROM sahara_leggings_28_rows "
                    "WHERE old_price != '' AND new_price != ''"
                ),
            ),
        ],
    )

    assert "FOREACH_BODY_GOAL_QUERY_ROW_PREDICATE" not in _codes(program)


def test_body_present_executes_body_not_subdecompose():
    # Design B (one-call sub-function): body_goal is a docstring, body carries the templated steps.
    # The body must execute by substitution; subdecompose_fn must NOT be called.
    program = Program(
        goal="g",
        statements=[ForEach(
            var="row", into="materials", returns=["Name"],
            body_goal="找 {row[Name]} 的父产品读主材质",  # docstring only
            body=[Read(var="d", name="读 {row[Name]} 父产品材质",  returns=["material"])],
        )],
    )
    subcalls: list[str] = []

    def collect_fn(target, returns, limit=None):
        return [{"Name": "Minerva"}, {"Name": "Eos"}]

    def execute(run: Run) -> RunResult:
        if run.kind == "read":
            brand = "Minerva" if "Minerva" in run.name else "Eos"
            return RunResult(completed=True, reads={"material": _MATERIAL_OF[brand]})
        return RunResult(completed=True)

    interp = Interpreter(
        program, collect_fn=collect_fn,
        subdecompose_fn=lambda g: subcalls.append(g) or Program(statements=[]),
    )
    drive(interp, execute)
    assert subcalls == [], "Design B must execute the templated body, not call subdecompose"
    assert interp.env["materials"].rows == [
        {"Name": "Minerva", "material": "Cotton"},
        {"Name": "Eos", "material": "Fleece"},
    ]


def test_body_goal_without_subdecompose_fn_fails_honestly():
    # No subdecompose_fn wired → the foreach can't run the sub-goal; publish an incomplete table
    # (not a silent empty one) so a downstream data_query surfaces the gap.
    program = _body_goal_program()
    interp = Interpreter(
        program,
        collect_fn=lambda t, r, limit=None: [{"Name": "Minerva LumaTech V-Tee-XS-Blue"}],
        subdecompose_fn=None,
    )
    drive(interp, lambda run: RunResult(completed=True))
    assert interp.env["materials"].completed is False
    assert interp.finish_incomplete is True


def test_body_goal_one_level_only():
    # A sub-program may not itself spawn another body_goal sub-goal (depth-1). The nested one must
    # not run: it returns an incomplete table rather than recursing.
    program = _body_goal_program()

    def subdecompose_fn(goal: str) -> Program:
        # malicious/over-eager sub-program: a nested body_goal foreach
        return Program(statements=[
            ForEach(var="x", into="inner", returns=["v"],
                    body_goal="对 {x[Name]} 再做一次子目标"),
        ])

    interp = Interpreter(
        program,
        collect_fn=lambda t, r, limit=None: [{"Name": "Minerva LumaTech V-Tee-XS-Blue"}],
        subdecompose_fn=subdecompose_fn,
    )
    drive(interp, lambda run: RunResult(completed=True))
    # the inner foreach was driven (depth 1) but its body_goal could NOT spawn another sub-goal
    assert interp.env["inner"].completed is False


# ── validator guards ────────────────────────────────────────────────────────────
def _codes(program: Program) -> set:
    return {i.code for i in validate_program(program)}


def test_validator_body_goal_with_body_is_docstring_ok():
    # body_goal + body together = a docstring on a templated sub-function (Design B); NOT an error.
    # The agentic-only checks (returns/template) don't apply because the body carries the steps.
    p = Program(statements=[ForEach(
        var="row", returns=["Name"], body_goal="找 {row[Name]} 的父产品读材质",
        body=[Read(var="d", name="打开 {row[Name]} 的父产品",  returns=["material"])],
    )])
    codes = _codes(p)
    assert not any(c.startswith("FOREACH_BODY_GOAL") for c in codes), codes


def test_validator_body_goal_requires_returns():
    p = Program(statements=[ForEach(var="row", body_goal="读 {row[Name]} 的材质")])
    assert "FOREACH_BODY_GOAL_MISSING_RETURNS" in _codes(p)


def test_validator_body_goal_requires_row_template():
    p = Program(statements=[ForEach(
        var="row", returns=["material"], body_goal="读这一行的材质",  # no {row[...]} template
    )])
    assert "FOREACH_BODY_GOAL_NO_ROW_TEMPLATE" in _codes(p)


def test_validator_body_goal_well_formed_passes():
    p = Program(statements=[
        ForEach(var="row", returns=["material"], into="materials",
                body_goal="找到 {row[Name]} 的父配置型产品，读主材质 → material"),
        Finish(message="{q[result]}"),
    ])
    codes = _codes(p)
    assert not any(c.startswith("FOREACH_BODY_GOAL") for c in codes), codes
