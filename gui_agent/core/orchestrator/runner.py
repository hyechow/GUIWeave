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

from typing import Any, Callable, Generator, Optional

from pydantic import BaseModel, Field

from .program import (
    BARE_REF_RE, TEMPLATE_RE, Call, Compute, Cond, Finish, ForEach, FunctionDef, If, Program, Run,
    RunResult,
)
from .safe_eval import SafeEvalError, safe_eval

# Drive one milestone (one Run spec) to a terminal state and return its structured result.
MilestoneExecutor = Callable[[Run], RunResult]


class _ScalarRead(str):
    """A single-field read var in compute scope: IS the value it read (str ops / arithmetic-coerce
    work directly) while still answering var['field'] subscripts. Multi-field vars stay plain dicts
    (using them as a scalar is genuinely ambiguous → honest error)."""

    def __new__(cls, value: str, field: str):
        obj = super().__new__(cls, value)
        obj._field = field
        return obj

    def __getitem__(self, key):  # noqa: D105 — var['field'] → value; other str indexing unchanged
        if isinstance(key, str):
            if key == self._field:
                return str(self)
            raise KeyError(key)
        return super().__getitem__(key)


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

    def __init__(self, program: Program, collect_fn=None, subdecompose_fn=None, expand_fn=None,
                 select_fn=None) -> None:
        self._program = program
        # Optional collect_fn for browser path: callable(target: str, returns: list[str]) -> list[dict] | None.
        # When ForEach.over is empty, _foreach calls this to retrieve rows directly via DOM/AX tree.
        # None = legacy path (over must point to an env var with .rows).
        self._collect_fn = collect_fn
        # Optional subdecompose_fn for agentic per-row sub-goals: callable(goal: str) -> Program.
        # When ForEach.body_goal is set, _foreach renders the sub-goal with the row and decomposes
        # it fresh per row, then `yield from`s the sub-program's Runs so the engine drives them as
        # full milestones (plan/replan/checker). None = no sub-goal support (body_goal can't run).
        self._subdecompose_fn = subdecompose_fn
        # Optional expand_fn — progressive-orchestration checkpoint expansion: callable(body_goal,
        # loop_var, rows, returns) -> ForeachExpansion | None. Tried ONCE at foreach entry when the
        # REAL rows are in hand: membership judged against actual data (selection AS data) + one
        # shared concrete body, then rows execute deterministically. None result (or no expand_fn)
        # falls back to per-row subdecompose — expansion is strictly an upgrade path.
        self._expand_fn = expand_fn
        # Optional select_fn — selection-ONLY checkpoint (preferred progressive form): callable
        # (member_desc, rows) -> list[int] | None. Used when ForEach carries member_desc + an
        # explicit t=0 body: the body was authored under the mature decomposer prompt and full
        # gates (offline-verifiable); only the membership decision — the part that genuinely needs
        # runtime data — is deferred. None result keeps all rows (t=0 body must then self-filter).
        self._select_fn = select_fn
        # Depth guard: a per-row sub-program may NOT itself spawn another body_goal sub-goal
        # (one level only). Incremented while driving a sub-program's block.
        self._subgoal_depth = 0
        self.env: dict[str, RunResult] = {}
        self.run_log: list[RunRecord] = []
        # First-class functions: name → FunctionDef (decomposed ONCE with main, called N times).
        self._functions: dict[str, FunctionDef] = {fn.name: fn for fn in (program.functions or [])}
        # Scalar scope for params + Compute results, referenced by bare `{name}`. A function call
        # swaps in a fresh frame (proper lexical scope: a function sees only its params + its own
        # computes, not the caller's). Bounded call depth guards runaway recursion.
        self._scalars: dict[str, str] = {}
        self._call_depth = 0
        # Set True by the Finish branch when the reached finish cites a read whose reads
        # were entirely empty — the run finished but its answer is hollow. See OrchestratorResult.
        self.finish_incomplete: bool = False
        # foreach `into` table vars — the ONLY env rows exposed to a data_query as a source. The
        # iteration source a foreach iterates also carries .rows, but those are the raw iteration items
        # (e.g. list ids with no detail fields) — exposing them pollutes the data_query source with a
        # half-empty table that confuses the repair.
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
            elif isinstance(s, Compute):
                # Pure compute — evaluate deterministically, bind a scalar. NOT a milestone (no yield).
                self._compute(s)
            elif isinstance(s, Call):
                reply = yield from self._call(s)
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
        """Run `loop.body` once per row of the collected rows, binding env[loop.var] to the row, and
        AUTO-accumulate each iteration (the row's fields + every body return field) into a materialized
        table published as env[loop.into]. Yields each body Run so the engine drives it as a milestone
        — the live loop and the synchronous `drive` both work unchanged. Returns a terminal reply if
        the body finishes/fails, else None.

        Row source priority:
        1. loop.over non-empty: env[loop.over].rows (legacy path — an env var with collected rows).
        2. self._collect_fn non-None: browser path — call collect_fn(target, returns) to get rows via DOM/AX.
        3. fallback: empty rows (nothing to iterate).
        """
        # Two body shapes:
        #   • body_goal WITHOUT body  → agentic per-row sub-goal: decomposed fresh per row at
        #     runtime. `returns` is the per-row CONTRACT (e.g. material), so the grid columns to
        #     gather are the row fields the sub-goal templates (`{var[field]}`).
        #   • body (with body_goal as an optional docstring) → a templated sub-function authored
        #     once in the main decompose; executed per row by {var[field]} substitution (no runtime
        #     decompose). This is the normal foreach: `returns` are the grid collect columns.
        agentic_subgoal = bool(loop.body_goal) and not loop.body
        alias_var: Optional[str] = None
        if agentic_subgoal:
            collect_cols = sorted({
                f.strip() for v, f in TEMPLATE_RE.findall(loop.body_goal) if v == loop.var
            })
            if not collect_cols:
                # Loop-var drift: the body_goal templates ONE consistent other name (var=item,
                # body_goal writes {row[sku]}) — mechanically unambiguous, so alias instead of
                # collecting zero columns → zero rows → a false "complete" (live 778 run 000715:
                # validator flagged it but the decompose retry budget ran out and the plan shipped).
                _names = {v for v, _ in TEMPLATE_RE.findall(loop.body_goal)}
                if len(_names) == 1:
                    alias_var = _names.pop()
                    collect_cols = sorted({
                        f.strip() for v, f in TEMPLATE_RE.findall(loop.body_goal) if v == alias_var
                    })
                    print(f"  [Foreach] 循环变量别名:body_goal 引用「{alias_var}」≠ 声明的「{loop.var}」,已机械对齐")
            if not collect_cols:
                # No row binding at all — the per-row sub-goal would run IDENTICALLY for every row.
                # Fail honestly instead of collecting nothing and reporting completion.
                self.finish_incomplete = True
                into0 = loop.into or f"{loop.var}s"
                self.env[into0] = RunResult(
                    completed=False, rows=[],
                    summary="body_goal 未引用任何行字段模板（{" + loop.var + "[字段]}）——无法按行采集/迭代",
                )
                self._materialized_vars.add(into0)
                self.run_log.append(RunRecord(
                    name=f"foreach {loop.var} (body_goal 无行绑定)", var=into0, result=self.env[into0]))
                return None
        else:
            collect_cols = list(loop.returns)
        rows: list[dict[str, str]] = []
        src = self.env.get(loop.over) if loop.over else None
        if src is not None and src.rows:
            # Legacy path: a preceding step already populated this var with rows (iPhone/Android).
            rows = list(src.rows)
        elif self._collect_fn is not None:
            # Browser path: collect rows from the current page via DOM/AX tree.
            # Also covers old-style foreach whose over var exists but carries no rows (e.g. when
            # the preceding row-collection read was silently dropped on schema upgrade).
            collected = self._collect_fn(loop.target, list(collect_cols), limit=loop.limit)
            rows = collected if collected is not None else []
        if loop.limit and rows:
            rows = rows[: loop.limit]
        into = loop.into or f"{loop.var}s"
        # Platform-general column-completeness safety net. A declared returns column that is
        # absent (no key) from EVERY collected row means the source grid never rendered it and
        # the collector silently dropped it — feeding an empty grouping/key column to a
        # downstream data_query yields a silent wrong/empty answer (burned WebArena task 63:
        # foreach declared Customer Email but the Orders grid didn't render it). "Absent as a
        # key in all rows" is the precise signal: a legitimately-blank-but-rendered column keeps
        # its key (= ""), so this never mis-fires on those. Browser tries to self-heal upstream
        # (Columns control) before we get here; this fails honestly when heal was impossible or
        # the platform has no such control, so the run surfaces the gap instead of answering on
        # missing data.
        if rows and collect_cols:
            uncovered = [f for f in collect_cols if all(f not in row for row in rows)]
            if uncovered:
                self.finish_incomplete = True
                self.env[into] = RunResult(
                    completed=False, rows=[],
                    summary=f"采集列缺失：声明的 {uncovered} 未出现在任何行（网格未渲染该列，已丢列）",
                )
                self._materialized_vars.add(into)
                self.run_log.append(RunRecord(
                    name=f"foreach {loop.var} in {loop.over}", var=into, result=self.env[into],
                ))
                return None
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
        # Selection-only checkpoint (preferred progressive form): member_desc + explicit t=0 body.
        # ONE selection call filters the REAL rows; the t=0-authored body (mature prompt, full
        # gates) runs on the members. None ⇒ keep all rows (body executes per row unfiltered —
        # same as pre-member_desc behaviour, strictly no downgrade).
        expanded_note = ""
        if loop.member_desc and loop.body and self._select_fn is not None and self._subgoal_depth == 0:
            sel: Optional[list[int]] = None
            try:
                sel = self._select_fn(loop.member_desc, rows)
            except Exception:  # noqa: BLE001 — selection must never be a new failure mode
                sel = None
            if sel is not None:
                total = len(rows)
                rows = [rows[i] for i in sel if 0 <= i < total]
                expanded_note = f"检查点圈选:{len(rows)}/{total} 行属于「{loop.member_desc[:30]}」"
                print(f"  [Select] {expanded_note}")
                if not rows:
                    self.env[into] = RunResult(completed=True, rows=[],
                                               summary=f"{expanded_note}(无成员,集合为空)")
                    self._materialized_vars.add(into)
                    self.run_log.append(RunRecord(
                        name=f"foreach {loop.var} (selected)", var=into, result=self.env[into]))
                    return None
        # Checkpoint expansion (progressive orchestration): with the REAL rows now in hand, ONE
        # refinement call selects the member rows (judgment as data — the decision t=0 literal
        # guessing got 0-for-all on live 778) and emits one shared concrete body; the loop below
        # then runs deterministically. Falls back to per-row subdecompose on None.
        if agentic_subgoal and self._expand_fn is not None and self._subgoal_depth == 0:
            expansion = None
            try:
                expansion = self._expand_fn(loop.body_goal, loop.var, rows, list(loop.returns))
            except Exception:  # noqa: BLE001 — expansion must never be a new failure mode
                expansion = None
            if expansion is not None:
                rows = [rows[i] for i in expansion.member_indices if 0 <= i < len(rows)]
                expanded_note = expansion.note or f"检查点展开:圈选 {len(rows)} 行"
                print(f"  [Expand] {expanded_note}")
                if not rows:
                    self.env[into] = RunResult(completed=True, rows=[],
                                               summary=f"{expanded_note}(无成员,集合为空)")
                    self._materialized_vars.add(into)
                    self.run_log.append(RunRecord(
                        name=f"foreach {loop.var} (expanded)", var=into, result=self.env[into]))
                    return None
                agentic_subgoal = False           # body is now concrete; no per-row decompose
                loop = loop.model_copy(update={"body": list(expansion.body), "body_goal": ""})
                body_read_vars = self._read_vars(loop.body)
        for row in rows:
            self.env[loop.var] = RunResult(completed=True, reads=dict(row))
            if alias_var:
                self.env[alias_var] = self.env[loop.var]   # drifted body_goal name resolves too
            if agentic_subgoal:
                # Agentic per-row sub-goal: render with the row, decompose fresh, drive its Runs
                # as full milestones (yield from → engine plans/replans each), merge its produced
                # fields back into the row.
                sub_stmts, sub_read_vars = self._subgoal_statements(loop)
                if sub_stmts is None:
                    self.finish_incomplete = True
                    self.env[into] = RunResult(
                        completed=False, rows=[],
                        summary="body_goal 无法分解（未接入 subdecompose_fn 或超出一层嵌套）",
                    )
                    self._materialized_vars.add(into)
                    self.run_log.append(RunRecord(
                        name=f"foreach {loop.var} (body_goal)", var=into, result=self.env[into]))
                    return None
                self._subgoal_depth += 1
                try:
                    reply = yield from self._block(sub_stmts)
                finally:
                    self._subgoal_depth -= 1
                if reply is not None:
                    return reply
                body_read_vars = sub_read_vars
            else:
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
                                   summary=f"采集 {len(accumulated)} 行（foreach {loop.var}）"
                                           + (f"；{expanded_note}" if expanded_note else ""))
        self._materialized_vars.add(into)
        self.run_log.append(RunRecord(
            name=f"foreach {loop.var} in {loop.over}", var=into, result=self.env[into],
        ))
        return None

    def _subgoal_statements(self, loop: ForEach) -> tuple[Optional[list], list[str]]:
        """Decompose this foreach's `body_goal` for the CURRENT row (already bound in env) into a
        sub-program, returning (its statements, its result vars to merge). (None, []) when it can't
        run: no subdecompose_fn wired, already one level deep (one-level-only), or decompose failed.
        The row value is rendered INTO the goal text, so the sub-program is concrete (no per-row
        templating needed inside it)."""
        if self._subdecompose_fn is None or self._subgoal_depth > 0:
            return None, []
        sub_goal = self._render(loop.body_goal)
        try:
            sub_prog = self._subdecompose_fn(sub_goal)
        except Exception:  # noqa: BLE001 — a failed sub-decompose must not crash the parent run
            sub_prog = None
        if sub_prog is None or not getattr(sub_prog, "statements", None):
            return None, []
        # Strip the sub-program's Finish statements: a per-row sub-goal's finish means "this ROW is
        # done", not "the TASK is done" — left in place it became the _block reply and terminated
        # the whole program after member #1 (live 778 run 234512: 1 of 3 variants saved, then the
        # run exited SUCCESS). The parent skeleton owns the task-level finish.
        stmts = self._strip_finish(list(sub_prog.statements))
        if not stmts:
            return None, []
        return stmts, self._read_vars(stmts)

    @staticmethod
    def _strip_finish(stmts: list) -> list:
        out = []
        for s in stmts:
            if isinstance(s, Finish):
                continue
            if isinstance(s, If):
                s = s.model_copy(update={
                    "then": Interpreter._strip_finish(s.then),
                    "otherwise": Interpreter._strip_finish(s.otherwise),
                })
            out.append(s)
        return out

    # ── pure compute + function calls ─────────────────────────────────────
    def _compute(self, c: Compute) -> None:
        """Evaluate a Compute's restricted expression over the current scalar scope and bind the
        result as a scalar. A failure (bad expr / index) leaves the scalar empty and logs honestly —
        a downstream `{var}` then renders empty, so a milestone that needs it fails fast in _fill.

        Accept the SAME `{name}` template convention the rest of the DSL uses for scalar refs: the
        decomposer reaches for `{sku}` (as it does in every milestone name) instead of bare `sku`,
        but to safe_eval `{sku}` is a one-element SET literal → SafeEvalError → silently-empty result
        (live 185: base_sku came out "", so the search milestone ran with an empty keyword). Strip the
        braces around bare identifiers so `{sku}.rsplit(...)` and `sku.rsplit(...)` are equivalent.
        Same for the field form: `{p[price]} * 0.865` (offline 778: parsed as a set literal →
        SafeEvalError → "") normalizes to `p['price'] * 0.865`."""
        expr = TEMPLATE_RE.sub(lambda m: f"{m.group(1)}[{m.group(2)!r}]", c.expr)
        expr = BARE_REF_RE.sub(r"\1", expr)
        # Scope from env RunResults' reads, in BOTH shapes the decomposer nondeterministically writes:
        # the field name bare (`current_price`) AND the var-as-dict (`variant_row['current_price']`).
        # Without this, a numeric derivation from a read value raised 未知变量 → silently "" → the fill
        # milestone lost its concrete value and the planner hallucinated one (WebArena 778: bare
        # current_price → new_price "" → fail-fast; earlier subscript form → typed 200.00). Scalars
        # (params + prior computes) win on collision.
        scope: dict[str, Any] = {}
        for _v, _rv in self.env.items():
            for _field, _val in _rv.reads.items():
                scope[_field] = _val           # flat field ref (last read wins)
            if len(_rv.reads) == 1:
                # A single-field read var is usable BOTH as the scalar it read AND as var['field']:
                # the decomposer treats it as a scalar (live 778 run 233801 wrote
                # `old_price_str.replace('$','')` where old_price_str was a one-field read var —
                # scope held a dict → 不允许的方法调用 → "" → fail-fast with the right value in hand).
                ((_only_field, _only_val),) = _rv.reads.items()
                scope[_v] = _ScalarRead(_only_val, _only_field)
            else:
                scope[_v] = dict(_rv.reads)     # var-as-dict for `{var}['field']`; var name wins over a same-named field
        scope.update(self._scalars)
        try:
            val = safe_eval(expr, scope)
            self._scalars[c.var] = "" if val is None else str(val)
        except SafeEvalError as e:
            self._scalars[c.var] = ""
            self.run_log.append(RunRecord(
                name=f"compute {c.var} = {c.expr}", var=c.var,
                result=RunResult(completed=False, summary=f"compute 求值失败: {e}")))
            print(f"  [Compute] {c.var} = {c.expr} 求值失败: {e}")

    _MAX_CALL_DEPTH = 6

    def _call(self, call: Call) -> Generator[Run, RunResult, Optional[str]]:
        """Invoke a FunctionDef: render args in the caller scope, run the body in a FRESH scalar
        frame (the function sees only its params + its own computes), then bind the declared returns
        into the caller's env under `call.var`. The body's Runs are `yield from`'d so the engine
        drives them as full milestones — same generator path as the top program."""
        fn = self._functions.get(call.func)
        if fn is None:
            fail = RunResult(completed=False, failed=True, summary=f"未定义的函数「{call.func}」")
            self.run_log.append(RunRecord(name=f"call {call.func}", var=call.var, result=fail))
            return f"调用了未定义的函数「{call.func}」"
        if self._call_depth >= self._MAX_CALL_DEPTH:
            fail = RunResult(completed=False, failed=True, summary=f"函数调用嵌套超过 {self._MAX_CALL_DEPTH} 层")
            self.run_log.append(RunRecord(name=f"call {call.func}", var=call.var, result=fail))
            return f"函数调用嵌套过深（{call.func}）"
        # Render args in the CALLER's scope (env {x[f]} + caller scalars {y}), bind to params.
        bound = {p: self._render(call.args.get(p, "")) for p in fn.params}
        saved_scalars = self._scalars
        self._scalars = bound
        self._call_depth += 1
        try:
            reply = yield from self._block(fn.body)
            collected = self._collect_returns(fn)
        finally:
            self._call_depth -= 1
            self._scalars = saved_scalars
        if reply is not None:
            return reply  # a finish/failure inside the function terminates the whole program
        if call.var:
            self.env[call.var] = RunResult(completed=True, reads=collected)
        return None

    def _collect_returns(self, fn: FunctionDef) -> dict[str, str]:
        """A function's declared returns, gathered from its frame: scalar (Compute result) first,
        else the most recent body milestone read of that field. Missing → "" (honest blank)."""
        collected: dict[str, str] = {}
        body_vars = self._read_vars(fn.body)
        for r in fn.returns:
            if r in self._scalars:
                collected[r] = self._scalars[r]
                continue
            for v in reversed(body_vars):
                rv = self.env.get(v)
                if rv is not None and (rv.reads.get(r, "") or "").strip():
                    collected[r] = rv.reads[r]
                    break
            collected.setdefault(r, "")
        return collected

    @staticmethod
    def _read_vars(stmts: list) -> list[str]:
        """Body result vars whose reads get auto-accumulated per iteration (program order)."""
        out: list[str] = []
        for s in stmts:
            if isinstance(s, Run) and s.var and (s.kind == "data_query" or s.returns):
                out.append(s.var)
            elif isinstance(s, Call) and s.var:
                out.append(s.var)
            elif isinstance(s, If):
                out.extend(Interpreter._read_vars(s.then))
                out.extend(Interpreter._read_vars(s.otherwise))
        return out

    def _eval(self, cond: Cond) -> bool:
        rv = self.env.get(cond.var)
        if rv is not None:
            actual = rv.reads.get(cond.field, "").strip()
        else:
            # Scalar cond (Python-surface compiles a free-form `if <expr>:` to a Compute scalar +
            # this cond with field == var): resolve from the compute/param scope.
            actual = str(self._scalars.get(cond.var, "")).strip()
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
        # Bare `{base}` scalar refs in the name (function params / Compute results) also identify the
        # per-row target — anchor the acceptance gate to them too, so a leftover page from the prior
        # iteration can't satisfy a generic gate (live 094903: the Eos call read Minerva's stale
        # edit page because the gate was "已进入某 Configurable 编辑页", not "...{base}...").
        for m in BARE_REF_RE.finditer(run.name or ""):
            if m.group(1) not in self._scalars:
                continue  # stray bare {foo} → existing botched-ref handling, not a scalar gap
            v = (self._scalars.get(m.group(1), "") or "").strip()
            if v:
                target_ref_values.append(v)
            else:
                # A KNOWN scalar (function param / Compute result) that resolved empty: the action
                # target has no concrete value — fail fast like an empty {var[field]} name ref, so a
                # failed compute can't silently degrade the milestone to a generic name the planner
                # then fills with a hallucinated value (WebArena 778: typed 200.00).
                missing.append(f"{{{m.group(1)}}}")
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

        # Scalars first (bare `{name}` for params + Compute results); then {var[field]} from env.
        # Disjoint patterns ({base} has no `[`), so order is safe. Only names actually in scalar
        # scope are substituted — a stray bare {foo} is left for the existing botched-ref handling.
        def _sub_scalar(m) -> str:
            name = m.group(1)
            return self._scalars[name] if name in self._scalars else m.group(0)

        out = BARE_REF_RE.sub(_sub_scalar, template) if self._scalars else template
        return TEMPLATE_RE.sub(_sub, out)

    def materialized_tables(self) -> list[dict]:
        """foreach `into` tables (accumulated per-iteration rows) as data_query-shaped snapshots
        {caption, headers, rows} — so a data_query after a foreach can query the collected set. ONLY
        the into tables (not the row-collection iteration sources, which carry raw .rows too); caption =
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
