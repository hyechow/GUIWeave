# -*- coding: utf-8 -*-
"""Checkpoint-expansion hook tests: at foreach entry with rows in hand, ONE expansion call selects
member rows (judgment as data) + emits one shared concrete body; per-row subdecompose stays as the
fallback. All deterministic — expand_fn is mocked; expansion._parse_and_validate is unit-tested."""
from __future__ import annotations

from gui_agent.core.orchestrator import (
    Compute, Finish, ForEach, Interpreter, Program, Run, RunResult, drive,
)
from gui_agent.core.orchestrator.expansion import ForeachExpansion, _parse_and_validate

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

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        if run.returns == ["current_price"]:
            return RunResult(completed=True, reads={"current_price": "75.00"})
        return RunResult(completed=True)

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
    assert into.completed and len(into.rows) == 2 and "检查点展开" in into.summary


def test_expansion_none_falls_back_to_per_row_subdecompose():
    calls = {"sub": 0}

    def subdecompose(goal):
        calls["sub"] += 1
        return Program(goal=goal, statements=[
            Run(kind="action", name="按子目标处理该行", success_condition="已处理"),
        ])

    seen: list[str] = []

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        return RunResult(completed=True)

    interp = Interpreter(PROG.model_copy(deep=True), collect_fn=_collect,
                         subdecompose_fn=subdecompose, expand_fn=lambda *a: None)
    drive(interp, execute)
    assert calls["sub"] == 4 and len(seen) == 4    # per-row path unchanged


def test_expansion_empty_selection_publishes_empty_table_and_continues():
    def expand_fn(body_goal, loop_var, rows, returns):
        return ForeachExpansion(member_indices=[], body=EXP_BODY, note="检查点展开:圈选 0/4 行")

    executed: list[str] = []

    def execute(run: Run) -> RunResult:
        executed.append(run.name)
        return RunResult(completed=True)

    interp = Interpreter(PROG.model_copy(deep=True), collect_fn=_collect,
                         subdecompose_fn=None, expand_fn=expand_fn)
    reply = drive(interp, execute)
    assert executed == []                          # no member → no body execution
    assert interp.env["rows"].completed and interp.env["rows"].rows == []
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

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        if run.returns == ["current_price"]:
            return RunResult(completed=True, reads={"current_price": "75.00"})
        return RunResult(completed=True)

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

    def execute(run: Run) -> RunResult:
        seen.append(run.name)
        return RunResult(completed=True)

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

    def execute(run: Run) -> RunResult:
        executed.append(run.name)
        return RunResult(completed=True)

    interp = Interpreter(prog, collect_fn=_collect, select_fn=lambda *a: [])
    reply = drive(interp, execute)
    assert executed == [] and interp.env["rows"].rows == []
    assert not interp.finish_incomplete and reply is not None
