"""Deterministic wiring for the re-decompose progress snapshot (summarize_progress).

Re-decompose's target is the UNEXECUTED statements, with the executed ones as experience (the
"重编排是有状态记忆的编排" requirement). summarize_progress derives both from the interpreter's
run_log + the program; this pins that split so a refactor can't silently turn re-decompose back into
a fresh-from-homepage full decompose."""

from gui_agent.core.orchestrator import (
    Finish,
    Program,
    Run,
    RunRecord,
    StatementOutcome,
    summarize_progress,
)
from gui_agent.core.orchestrator.program import Read


def _program() -> Program:
    return Program(
        goal="g",
        statements=[
            Run(name="进入 Reviews 页", success_condition="列表可见", kind="navigation"),
            Run(name="用 Olivia 筛选 Product 列", success_condition="筛出候选", kind="filter"),
            Run(name="添加筛选 Rating<=3", success_condition="只剩≤3", kind="filter"),
            Read(var="r", name="读取昵称", success_condition="",  returns=["nickname"]),
            Finish(message="{r[nickname]}"),
        ],
    )


def _run_log() -> list[RunRecord]:
    return [
        RunRecord(name="进入 Reviews 页", result=StatementOutcome.completed("已到 Reviews")),
        RunRecord(
            name="用 Olivia 筛选 Product 列",
            result=StatementOutcome.completed("3 条候选", reads={"count": "3"}),
        ),
    ]


def test_experience_lists_executed_with_outcomes():
    experience, _ = summarize_progress(_program(), _run_log(), current_run=None)
    assert "✓ 进入 Reviews 页" in experience
    assert "用 Olivia 筛选 Product 列" in experience
    assert "3 条候选" in experience
    assert "count=3" in experience            # read values carried as experience
    # not-yet-executed statements must NOT appear in experience
    assert "Rating<=3" not in experience


def test_remaining_excludes_executed_and_heads_with_current():
    program = _program()
    current = program.statements[2]           # the failed "Rating<=3" filter (abandoned mid-yield)
    assert isinstance(current, Run)
    _, remaining = summarize_progress(program, _run_log(), current_run=current)
    # current failed statement heads the remaining plan, flagged as the correction point
    assert remaining.splitlines()[0].startswith("1.")
    assert "Rating<=3" in remaining.splitlines()[0]
    assert "触发了上层纠正" in remaining.splitlines()[0]
    # the still-pending read is also remaining; executed ones are NOT
    assert "读取昵称" in remaining
    assert "进入 Reviews 页" not in remaining
    assert "用 Olivia 筛选 Product 列" not in remaining


def test_no_current_run_remaining_is_just_unexecuted():
    program = _program()
    _, remaining = summarize_progress(program, _run_log(), current_run=None)
    # no current → remaining is exactly the unexecuted statements, in program order
    assert "Rating<=3" in remaining
    assert "读取昵称" in remaining
    assert "进入 Reviews 页" not in remaining
    assert "触发了上层纠正" not in remaining
