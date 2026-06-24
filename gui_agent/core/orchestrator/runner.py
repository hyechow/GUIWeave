"""DSL interpreter = the milestone SEQUENCER (replaces the supervisor's walker/DAG).

The agent_loop IS the program runner: it owns the session + the turn loop, and each
milestone is a sub-loop (several turns driving one milestone to done). The DSL only
changes WHO decides the next milestone — the walker becomes this interpreter, which
threads each run's structured RunResult through variables / conditions / finish.

So the interpreter must be STEPPABLE, not a synchronous black box: executing a run() is
many turns of the engine, not one call. `Interpreter.steps()` is a generator — it yields
each Run for the engine to drive, receives the RunResult via send(), and returns the final
reply. Persisting every run's reads in env/run_log is why "the middle read it but the
output didn't know" disappears: the answer comes from the whole program, not the last loop.

    interp = Interpreter(program)
    gen = interp.steps()
    run = next(gen)
    while True:
        result = <drive milestone `run` to done>     # agent_loop sub-loop
        run = gen.send(result)                         # next Run
    # StopIteration.value = final reply

`drive()` / `ProgramRunner` are the synchronous convenience for tests + headless use.
"""

from __future__ import annotations

from typing import Callable, Generator, Optional

from pydantic import BaseModel, Field

from .program import TEMPLATE_RE, Cond, Finish, ForEach, If, Program, Run, RunResult

# Drive one milestone (one Run spec) to a terminal state and return its structured result.
MilestoneExecutor = Callable[[Run], RunResult]


class RunRecord(BaseModel):
    name: str
    var: Optional[str] = None
    result: RunResult


def _flatten_runs(stmts: list) -> list["Run"]:
    """Program order, DFS into if-branches; finishes/ifs themselves are not milestones."""
    out: list[Run] = []
    for s in stmts:
        if isinstance(s, Run):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_flatten_runs(s.then))
            out.extend(_flatten_runs(s.otherwise))
    return out


def summarize_progress(
    program: Program,
    run_log: list[RunRecord],
    current_run: Optional["Run"] = None,
) -> tuple[str, str]:
    """Render (prior_experience, remaining_plan) text for a mid-run re-decompose.

    Re-decompose's target is the UNEXECUTED milestones, with the executed ones as experience
    ([[progress-monitor-architecture]] / the user's "重编排是有状态记忆的编排"). We derive both from
    the interpreter's run_log (what completed, with outcomes) and the program statements not yet
    executed. The milestone that hit the correction (`current_run`) is abandoned mid-yield so it is
    NOT in run_log — it's surfaced at the head of the remaining plan (its goal still needs doing,
    via the directive's route)."""
    executed = {rec.name for rec in run_log}

    exp_lines: list[str] = []
    for rec in run_log:
        mark = "✓" if rec.result.completed and not rec.result.failed else "✗"
        line = f"{mark} {rec.name}"
        if rec.result.summary:
            line += f"（{rec.result.summary}）"
        reads = {k: v for k, v in (rec.result.reads or {}).items() if (v or "").strip()}
        if reads:
            line += " — 已读到：" + "；".join(f"{k}={v}" for k, v in reads.items())
        exp_lines.append(line)
    experience = "\n".join(exp_lines)

    remaining_runs = [
        r for r in _flatten_runs(program.statements)
        if r.name not in executed and (current_run is None or r.name != current_run.name)
    ]
    rem_lines: list[str] = []
    if current_run is not None:
        sc = f" —— 原验收：{current_run.success_condition}" if current_run.success_condition else ""
        rem_lines.append(f"1. [{current_run.kind}] {current_run.name}{sc}（← 在此步触发了上层纠正，需按纠正指令改走可行路线）")
    for i, r in enumerate(remaining_runs, start=len(rem_lines) + 1):
        sc = f" —— 验收：{r.success_condition}" if r.success_condition else ""
        rem_lines.append(f"{i}. [{r.kind}] {r.name}{sc}")
    remaining = "\n".join(rem_lines)
    return experience, remaining


class OrchestratorResult(BaseModel):
    reply: str
    failed: bool = False
    # True when the program reached a `finish` whose template referenced a read whose
    # RunResult.reads came back ENTIRELY empty (every field blank) — i.e. the finish
    # answered on a read that read nothing. Such a run reached the end but produced no
    # real answer, so callers must NOT treat it as goal_completed (see result.py). False
    # for programs that never hit finish (auto-summary) or whose cited reads had data.
    finish_incomplete: bool = False
    env: dict[str, RunResult] = Field(default_factory=dict)
    run_log: list[RunRecord] = Field(default_factory=list)


class Interpreter:
    """Steppable DSL interpreter. Drive it via steps() (a generator): it yields each Run
    and receives its RunResult via send(); steps() returns the final reply. env / run_log
    accumulate so the answer is built from the whole program's structured state."""

    def __init__(self, program: Program) -> None:
        self._program = program
        self.env: dict[str, RunResult] = {}
        self.run_log: list[RunRecord] = []
        # Set True by the Finish branch when the reached finish cites a read whose reads
        # were entirely empty — the run finished but its answer is hollow. See OrchestratorResult.
        self.finish_incomplete: bool = False
        # foreach `into` table vars — the ONLY env rows exposed to a data_query as a source. The
        # list_read SOURCE a foreach iterates also carries .rows, but those are the raw iteration items
        # (e.g. list ids with no detail fields) — exposing them pollutes the data_query source with a
        # half-empty table that confuses the repair (regression 20260622_214841: the list_read `r`
        # table's empty `rating` column made the query judge the source bad, though the accumulated
        # `reviews` table held the correct ratings).
        self._materialized_vars: set[str] = set()

    @property
    def failed(self) -> bool:
        return any(r.result.failed for r in self.run_log)

    def steps(self) -> Generator[Run, RunResult, str]:
        reply = yield from self._block(self._program.statements)
        return reply if reply is not None else self._auto_summary()

    def _block(self, stmts: list) -> Generator[Run, RunResult, Optional[str]]:
        """Execute a block; returns a terminal reply (finish / failure) or None if it ran
        to the end of the block without terminating."""
        for s in stmts:
            if isinstance(s, Run):
                s, missing = self._fill(s)  # resolve {var[field]} from env BEFORE the planner sees it
                if missing:
                    # The action TARGET (name) references a read value that came back empty —
                    # driving a gap-named milestone (『编辑机器人 ，设…』) would misfire. Fail fast
                    # with an honest reply instead of silently sending an empty reference to the
                    # planner (the decomposer's validate guard prevents *dangling* refs; this catches
                    # the read-returned-empty case at runtime).
                    fail = RunResult(
                        completed=False, failed=True,
                        summary=f"动作目标引用 {missing} 在运行时为空（前置 read 未读到对应值）",
                    )
                    self.run_log.append(RunRecord(name=s.name, var=s.var, result=fail))
                    return f"子任务「{s.name}」无法执行：{fail.summary}"
                result = yield s  # engine drives this milestone and send()s back its result
                self.run_log.append(RunRecord(name=s.name, var=s.var, result=result))
                if s.var:
                    self.env[s.var] = result
                if result.failed:
                    # MVP: a failed milestone stops the program and reports honestly.
                    return f"子任务「{s.name}」未能完成：{result.summary or '执行失败'}"
            elif isinstance(s, If):
                branch = s.then if self._eval(s.cond) else s.otherwise
                reply = yield from self._block(branch)
                if reply is not None:
                    return reply
            elif isinstance(s, ForEach):
                reply = yield from self._foreach(s)
                if reply is not None:
                    return reply
            elif isinstance(s, Finish):
                rendered = self._render(s.message)
                # Empty-read guard: if this finish cites a read variable whose RunResult.reads
                # is ENTIRELY blank (every requested field came back ""), the finish is answering
                # on a read that read nothing — e.g. a retrieve task where the target table was
                # off-screen / wrong page → structured_read returned "". Mark the
                # program finish_incomplete so goal_completed stays False (result.py). Rule is
                # whole-read, not per-ref: a multi-field read like {连通判定, 不可达原因} where only
                # 不可达原因 is blank (合法: 可达时为空) still has 连通判定 set → NOT flagged — so a
                # legitimate otherwise-branch finish "不可达原因：{d[不可达原因]}" is not mis-killed.
                cited_vars = {var for var, _field in TEMPLATE_RE.findall(s.message)}
                if cited_vars and any(
                    (rv := self.env.get(v)) is not None
                    and not any((val or "").strip() for val in rv.reads.values())
                    for v in cited_vars
                ):
                    self.finish_incomplete = True
                return rendered
        return None

    def _foreach(self, loop: ForEach) -> Generator[Run, RunResult, Optional[str]]:
        """Run `loop.body` once per row of env[loop.over].rows, binding env[loop.var] to the row, and
        AUTO-accumulate each iteration (the row's fields + every body return field) into a materialized
        table published as env[loop.into]. Yields each body Run so the engine drives it as a milestone
        — the live loop and the synchronous `drive` both work unchanged. Returns a terminal reply if
        the body finishes/fails, else None."""
        src = self.env.get(loop.over)
        rows = list(src.rows) if src is not None else []
        into = loop.into or f"{loop.var}s"
        body_read_vars = self._read_vars(loop.body)
        accumulated: list[dict[str, str]] = []
        if not rows:
            # Nothing discovered to iterate — publish an empty table so a following data_query sees an
            # (empty, but present and complete) source rather than a missing one.
            self.env[into] = RunResult(completed=True, rows=[], summary=f"{loop.over} 无可迭代行")
            self._materialized_vars.add(into)
            self.run_log.append(RunRecord(
                name=f"foreach {loop.var} in {loop.over}", var=into,
                result=self.env[into],
            ))
            return None
        for row in rows:
            self.env[loop.var] = RunResult(completed=True, reads=dict(row))
            reply = yield from self._block(loop.body)
            if reply is not None:
                return reply  # body finished/failed → terminate the program honestly
            merged: dict[str, str] = dict(row)
            for v in body_read_vars:
                rv = self.env.get(v)
                if rv is not None:
                    merged.update({k: val for k, val in rv.reads.items()})
            accumulated.append(merged)
        self.env[into] = RunResult(completed=True, rows=accumulated,
                                   summary=f"采集 {len(accumulated)} 行（foreach {loop.var}）")
        self._materialized_vars.add(into)
        self.run_log.append(RunRecord(
            name=f"foreach {loop.var} in {loop.over}", var=into, result=self.env[into],
        ))
        return None

    @staticmethod
    def _read_vars(stmts: list) -> list[str]:
        """Body result vars whose reads get auto-accumulated per iteration (program order)."""
        out: list[str] = []
        for s in stmts:
            if isinstance(s, Run) and s.var and (s.kind == "data_query" or s.returns):
                out.append(s.var)
            elif isinstance(s, If):
                out.extend(Interpreter._read_vars(s.then))
                out.extend(Interpreter._read_vars(s.otherwise))
        return out

    def _eval(self, cond: Cond) -> bool:
        rv = self.env.get(cond.var)
        actual = (rv.reads.get(cond.field, "") if rv else "").strip()
        target = cond.value.strip()
        if cond.cmp == "==":
            return actual == target
        if cond.cmp == "!=":
            return actual != target
        if cond.cmp == "exists":
            return bool(actual)
        if cond.cmp == "empty":
            return not actual
        if cond.cmp == "contains":
            return bool(target) and target in actual
        if cond.cmp == "not_contains":
            return bool(target) and target not in actual
        values = [v.strip() for v in cond.values if v.strip()]
        if not values and target:
            values = [target]
        if cond.cmp == "in":
            return actual in values
        if cond.cmp == "not_in":
            return actual not in values
        return False

    def _fill(self, run: Run) -> tuple[Run, list[str]]:
        """Resolve {var[field]} refs in a Run's text from env BEFORE it reaches the planner.

        Read-then-reference (rule 10): an action the decomposer authored as『打开工单 {t[工单号]}』
        becomes『打开工单 WO-2024-007』so it targets the concrete entity a prior read identified —
        robust even when the list holds siblings, not just the only-row-on-screen. Same templater
        as finish (_render); env is already populated because the read runs — and send()s its
        RunResult back — before this Run is yielded.

        Returns (filled_run, missing): `missing` lists refs in the NAME (the action TARGET) that
        resolved to empty — the caller fails fast on those rather than driving a gap-named
        milestone. success_condition / read_spec render leniently: a gap in the acceptance gate or
        read guidance weakens them but doesn't misdirect the action, so it's not worth aborting on.
        Returns the run unchanged when nothing templated (the common case)."""
        missing: list[str] = []
        target_ref_values: list[str] = []
        for match in TEMPLATE_RE.finditer(run.name or ""):
            rv = self.env.get(match.group(1))
            value = (rv.reads.get(match.group(2).strip().strip("'\""), "") if rv else "").strip()
            if value:
                target_ref_values.append(value)
        name = self._render(run.name, missing)              # target → strict (collect empties)
        sc = self._render(run.success_condition)            # gate → lenient
        rs = self._render(run.read_spec)                    # read guidance → lenient
        if target_ref_values and not any(value in sc for value in target_ref_values):
            target_gate = f"必须对应子目标指定对象「{name}」"
            sc = f"{sc}（{target_gate}）" if sc else target_gate
        if name == run.name and sc == run.success_condition and rs == run.read_spec:
            return run, missing
        return run.model_copy(update={"name": name, "success_condition": sc, "read_spec": rs}), missing

    def _render(self, template: str, missing: Optional[list[str]] = None) -> str:
        def _sub(m) -> str:
            var, field = m.group(1), m.group(2).strip().strip("'\"")
            rv = self.env.get(var)
            val = rv.reads.get(field, "") if rv else ""
            if missing is not None and not val:
                missing.append(f"{var}[{field}]")
            return val

        return TEMPLATE_RE.sub(_sub, template)

    def materialized_tables(self) -> list[dict]:
        """foreach `into` tables (accumulated per-iteration rows) as data_query-shaped snapshots
        {caption, headers, rows} — so a data_query after a foreach can query the collected set. ONLY
        the into tables (not the list_read iteration sources, which carry raw .rows too); caption =
        the into var (a data_query alias); complete (not partial)."""
        out: list[dict] = []
        for var in self._materialized_vars:
            rv = self.env.get(var)
            if rv is None:
                continue
            headers: list[str] = []
            for r in rv.rows:
                for k in r:
                    if k not in headers:
                        headers.append(k)
            out.append({"caption": var, "headers": headers, "rows": list(rv.rows), "partial": False})
        return out

    def _auto_summary(self) -> str:
        """No explicit finish(): summarize from the persisted run results (reads first, else
        the last run's summary) — the orchestrator answers from the program's whole state."""
        reads = [
            "；".join(f"{k}：{v}" for k, v in r.result.reads.items() if v)
            for r in self.run_log
            if r.result.reads
        ]
        reads = [p for p in reads if p]
        if reads:
            return "；".join(reads)
        return (self.run_log[-1].result.summary if self.run_log else "") or "任务已执行完毕。"


def drive(interp: Interpreter, execute: MilestoneExecutor) -> str:
    """Synchronously drive an interpreter with a callable executor (tests / headless use).
    The GUI agent_loop drives interp.steps() turn-by-turn instead of using this."""
    gen = interp.steps()
    try:
        run = next(gen)
    except StopIteration as e:  # program with no run() (e.g. just finish / empty)
        return e.value or ""
    while True:
        result = execute(run)
        try:
            run = gen.send(result)
        except StopIteration as e:
            return e.value or ""


class ProgramRunner:
    """Synchronous convenience runner (tests / headless): build an Interpreter and drive it
    with the injected executor. The GUI agent_loop instead drives Interpreter.steps()
    directly, executing each yielded Run as a milestone sub-loop within its open session."""

    def __init__(self, execute: MilestoneExecutor) -> None:
        self._execute = execute

    def run(self, program: Program) -> OrchestratorResult:
        interp = Interpreter(program)
        reply = drive(interp, self._execute)
        return OrchestratorResult(
            reply=reply,
            failed=interp.failed,
            finish_incomplete=interp.finish_incomplete,
            env=dict(interp.env),
            run_log=list(interp.run_log),
        )
