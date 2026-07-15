# -*- coding: utf-8 -*-
"""Checkpoint-expansion hook tests: at foreach entry with rows in hand, ONE expansion call selects
member rows (judgment as data) + emits one shared concrete body; per-row subdecompose stays as the
fallback. All deterministic — expand_fn is mocked; expansion._parse_and_validate is unit-tested."""
from __future__ import annotations

from gui_agent.core.orchestrator import (
    Compute, Cond, Finish, ForEach, If, Interpreter, Program, Run, StatementOutcome, drive,
)
from gui_agent.core.orchestrator.expansion import ForeachExpansion, _parse_and_validate
from gui_agent.core.orchestrator.program import Read

ROWS = [
    {"id": "1842", "sku": "WP05-28-Gray", "price": "$75.00"},
    {"id": "1846", "sku": "WP05-29-Red", "price": "$75.00"},
    {"id": "1843", "sku": "WP05-28-Red", "price": "$75.00"},
    {"id": "1847", "sku": "WP05", "price": "$75.00"},
]

PROG = Program(goal="Reduce the price of size 28 Sahara leggings by 13.5%", statements=[
    ForEach(var="row", target="Sahara 变体行", returns=["new_price"],
            body_goal="判断 {row[sku]} 是否为 size 28 的变体；若是，读现价→算→更新并保存"),
    Finish(message="done"),
])

EXP_BODY = [
    Run(kind="navigation", var="d", name="打开变体 {row[sku]} 编辑页", success_condition="已进入编辑页",
        returns=["current_price"], read_spec="current_price: Price 输入框数值"),
    Compute(var="new_price", expr="round(float(d['current_price']) * 0.865, 2)"),
    Run(kind="action", name="将 Price 更新为 {new_price} 并保存", success_condition="保存成功"),
]


def _collect(target, cols, limit=None):
    return [dict(r) for r in ROWS]


def test_expansion_selects_members_and_drives_shared_body():
    calls = {"expand": 0, "sub": 0}

    def expand_fn(body_goal, loop_var, rows, returns):
        calls["expand"] += 1
        assert loop_var == "row" and len(rows) == 4
        return ForeachExpansion(member_indices=[0, 2], body=EXP_BODY, note="检查点展开:圈选 2/4 行")

    def subdecompose(goal):
        calls["sub"] += 1
        return None

    seen: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        seen.append(run.name)
        if run.returns == ["current_price"]:
            return StatementOutcome.completed("", reads={"current_price": "75.00"})
        return StatementOutcome.completed("")

    interp = Interpreter(PROG.model_copy(deep=True), collect_fn=_collect,
                         subdecompose_fn=subdecompose, expand_fn=expand_fn)
    drive(interp, execute)
    # one expansion call; per-row subdecompose never touched; only the 2 selected rows acted on
    assert calls == {"expand": 1, "sub": 0}
    assert seen == [
        "打开变体 WP05-28-Gray 编辑页", "将 Price 更新为 64.88 并保存",
        "打开变体 WP05-28-Red 编辑页", "将 Price 更新为 64.88 并保存",
    ]
    into = interp.env["rows"]
    assert into.is_completed and len(into.rows) == 2 and "检查点展开" in into.summary


def test_expansion_none_falls_back_to_per_row_subdecompose():
    calls = {"sub": 0}

    def subdecompose(goal):
        calls["sub"] += 1
        return Program(goal=goal, statements=[
            Run(kind="action", name="按子目标处理该行", success_condition="已处理"),
        ])

    seen: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        seen.append(run.name)
        return StatementOutcome.completed("")

    interp = Interpreter(PROG.model_copy(deep=True), collect_fn=_collect,
                         subdecompose_fn=subdecompose, expand_fn=lambda *a: None)
    drive(interp, execute)
    assert calls["sub"] == 4 and len(seen) == 4    # per-row path unchanged


def test_expansion_empty_selection_publishes_empty_table_and_continues():
    def expand_fn(body_goal, loop_var, rows, returns):
        return ForeachExpansion(member_indices=[], body=EXP_BODY, note="检查点展开:圈选 0/4 行")

    executed: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        executed.append(run.name)
        return StatementOutcome.completed("")

    interp = Interpreter(PROG.model_copy(deep=True), collect_fn=_collect,
                         subdecompose_fn=None, expand_fn=expand_fn)
    reply = drive(interp, execute)
    assert executed == []                          # no member → no body execution
    assert interp.env["rows"].is_completed and interp.env["rows"].rows == []
    assert not interp.finish_incomplete            # empty set is a legitimate outcome
    assert reply is not None                       # program flowed to finish


def test_parse_and_validate_good_and_bad_bodies():
    good = [
        {"op": "run", "kind": "navigation", "var": "d", "name": "打开 {row[sku]}",
         "success_condition": "已进入", "returns": ["current_price"], "read_spec": "读 Price"},
        {"op": "compute", "var": "np", "expr": "round(float(d['current_price']) * 0.865, 2)"},
        {"op": "run", "kind": "action", "name": "更新为 {np} 并保存", "success_condition": "已保存",
         "read_spec": None},                       # null-tolerance (json_object mode)
    ]
    stmts, issues = _parse_and_validate(good, "row", ["id", "sku", "price"], "降价")
    assert stmts is not None and not issues

    bad_op = [{"op": "teleport", "name": "x"}]
    stmts, issues = _parse_and_validate(bad_op, "row", ["sku"], "降价")
    assert stmts is None and issues

    empty, issues2 = _parse_and_validate([], "row", ["sku"], "降价")
    assert empty is None and issues2


def test_selection_only_runs_t0_body_on_members():
    # Preferred progressive form: member_desc + explicit t=0 body. One selection call filters rows;
    # the t=0-authored body (mature prompt, full gates) runs on members only.
    prog = Program(goal="Reduce the price of size 28 Sahara leggings by 13.5%", statements=[
        ForEach(var="row", target="Sahara 变体行", returns=["sku", "price"],
                member_desc="size 28 的 Sahara leggings 变体",
                body=[
                    Run(kind="navigation", var="d", name="打开变体 {row[sku]} 编辑页",
                        success_condition="已进入", returns=["current_price"], read_spec="读 Price"),
                    Compute(var="new_price", expr="round(float(d['current_price']) * 0.865, 2)"),
                    Run(kind="action", name="将 Price 更新为 {new_price} 并保存", success_condition="已保存"),
                ]),
        Finish(message="done"),
    ])
    calls = {"select": 0, "expand": 0}

    def select_fn(member_desc, rows):
        calls["select"] += 1
        assert "size 28" in member_desc and len(rows) == 4
        return [0, 2]

    def expand_fn(*a):
        calls["expand"] += 1
        return None

    seen: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        seen.append(run.name)
        if run.returns == ["current_price"]:
            return StatementOutcome.completed("", reads={"current_price": "75.00"})
        return StatementOutcome.completed("")

    interp = Interpreter(prog, collect_fn=_collect, expand_fn=expand_fn, select_fn=select_fn)
    drive(interp, execute)
    assert calls == {"select": 1, "expand": 0}          # selection-only; full expansion untouched
    assert seen == [
        "打开变体 WP05-28-Gray 编辑页", "将 Price 更新为 64.88 并保存",
        "打开变体 WP05-28-Red 编辑页", "将 Price 更新为 64.88 并保存",
    ]
    assert "检查点圈选" in interp.env["rows"].summary


def test_selection_none_keeps_all_rows_no_downgrade():
    prog = Program(goal="g", statements=[
        ForEach(var="row", target="行", returns=["sku"], member_desc="某集合",
                body=[Run(kind="action", name="处理 {row[sku]}", success_condition="ok")]),
        Finish(message="done"),
    ])
    seen: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        seen.append(run.name)
        return StatementOutcome.completed("")

    interp = Interpreter(prog, collect_fn=_collect, select_fn=lambda *a: None)
    drive(interp, execute)
    assert len(seen) == 4                                # all rows — pre-member_desc behaviour


def test_selection_empty_publishes_empty_table():
    prog = Program(goal="g", statements=[
        ForEach(var="row", target="行", returns=["sku"], member_desc="某集合",
                body=[Run(kind="action", name="处理 {row[sku]}", success_condition="ok")]),
        Finish(message="done"),
    ])
    executed: list[str] = []

    def execute(run: Run) -> StatementOutcome:
        executed.append(run.name)
        return StatementOutcome.completed("")

    interp = Interpreter(prog, collect_fn=_collect, select_fn=lambda *a: [])
    reply = drive(interp, execute)
    assert executed == [] and interp.env["rows"].rows == []
    assert not interp.finish_incomplete and reply is not None


def test_subgoal_finish_does_not_terminate_the_loop():
    # Live 778 run 234512: the per-row sub-program ended with its own Finish — it became the _block
    # reply and terminated the WHOLE program after member #1 (1 of 3 variants saved, exit SUCCESS).
    # A row-level finish means "this row is done"; the skeleton owns the task-level finish.
    prog = Program(goal="降价", statements=[
        ForEach(var="row", target="变体行", returns=["sku"],
                body_goal="对 {row[sku]} 读价改价保存"),
        Finish(message="全部完成"),
    ])
    def subdecompose(goal):
        return Program(goal=goal, statements=[
            Run(kind="action", name=f"处理 {goal[-18:]}", success_condition="ok"),
            Finish(message="该行已完成"),                      # ← must NOT end the parent loop
        ])
    seen: list[str] = []
    def execute(run: Run) -> StatementOutcome:
        seen.append(run.name)
        return StatementOutcome.completed("")
    interp = Interpreter(prog, collect_fn=_collect, subdecompose_fn=subdecompose)
    reply = drive(interp, execute)
    assert len(seen) == 4, seen                                # all 4 rows processed
    assert reply == "全部完成"                                 # skeleton finish, not the row's
    assert len(interp.env["rows"].rows) == 4


def test_subgoal_if_nested_finish_also_stripped():
    prog = Program(goal="g", statements=[
        ForEach(var="row", target="行", returns=["sku"], body_goal="对 {row[sku]} 条件处理"),
        Finish(message="done"),
    ])
    def subdecompose(goal):
        return Program(goal=goal, statements=[
            Read( var="v", name="读状态", returns=["s"], read_spec="读", success_condition="ok"),
            If(cond=Cond(var="v", field="s", cmp="==", value="skip"),
               then=[Finish(message="跳过")],                   # nested finish
               otherwise=[Run(kind="action", name="处理", success_condition="ok")]),
        ])
    seen: list[str] = []
    def execute(run: Run) -> StatementOutcome:
        seen.append(run.name)
        if run.var == "v":
            return StatementOutcome.completed("", reads={"s": "go"})
        return StatementOutcome.completed("")
    interp = Interpreter(prog, collect_fn=_collect, subdecompose_fn=subdecompose)
    reply = drive(interp, execute)
    assert seen.count("处理") == 4 and reply == "done"


def test_bodygoal_loopvar_drift_aliased_and_zero_binding_honest():
    # Live 778 run 000715: var=item but body_goal templates {row[sku]} — collect_cols came out
    # empty → 0 rows → "无可迭代行" reported COMPLETE. The drifted name is mechanically unambiguous
    # (exactly one templated name) → alias; a body_goal with NO row template fails honestly.
    prog = Program(goal="降价", statements=[
        ForEach(var="item", target="Sahara 行", returns=["sku"],
                body_goal="判断 {row[sku]} 是否 size 28;若是处理"),
        Finish(message="done"),
    ])
    calls: list[str] = []

    def subdecompose(goal):
        calls.append(goal)
        return Program(goal=goal, statements=[
            Run(kind="action", name="处理", success_condition="ok"),
        ])

    def execute(run: Run) -> StatementOutcome:
        return StatementOutcome.completed("")

    interp = Interpreter(prog, collect_fn=_collect, subdecompose_fn=subdecompose)
    drive(interp, execute)
    assert len(calls) == 4                                    # aliased: all 4 rows iterated
    assert "WP05-28-Gray" in calls[0]                         # {row[sku]} rendered per row
    assert not interp.finish_incomplete

    # no row template at all → honest incomplete, not a silent complete
    prog2 = Program(goal="降价", statements=[
        ForEach(var="item", target="行", returns=["sku"], body_goal="对每一行做处理"),
        Finish(message="done"),
    ])
    interp2 = Interpreter(prog2, collect_fn=_collect, subdecompose_fn=subdecompose)
    drive(interp2, execute)
    assert interp2.finish_incomplete
    assert not interp2.env["items"].is_completed
