"""Non-UI orchestrator primitive execution.

`read` and `data_query` are execution primitives, not UI actions. They consume the
current observation/table snapshot, record a non-interactive turn, and advance the
DSL interpreter without going through the supervisor/action-policy loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.engine import package_result, task_type_for, to_milestone
from gui_agent.core.orchestrator.program import Run
from gui_agent.core.run.turns import make_non_ui_turn
from gui_agent.core.schemas import Observation, PolicyContext


@dataclass
class NonUiDriveResult:
    """State changes from driving pending non-UI runs."""

    current_run: Run | None
    run_index: int
    notes_mark: int
    reply: str | None = None
    # Feasibility Guard (non-UI kick-back): set when a data_query/read step FAILED in a re-plannable way
    # (data source empty / mismatched with the task intent) — the loop turns this into a re-decompose
    # directive instead of plainly ending the run. None = no re-plannable non-UI failure.
    failure_evidence: str | None = None


@dataclass
class _RepairAttempt:
    reads: dict[str, str] | None
    sql: str
    reason: str
    source_issue: str = ""


def drive_pending_non_ui(
    *,
    current_run: Run | None,
    run_index: int,
    notes_mark: int,
    interpreter_steps: Any,
    bundle: Any,
    platform: Any,
    log_dir: Path,
    supervisor: Any,
    context: PolicyContext,
    save_context: Callable[[], None],
    say: Callable[[str], None],
    done_observation: Observation | None = None,
    observation_url: str | None = None,
    materialized_tables: "Callable[[], list[dict[str, Any]]] | None" = None,
) -> NonUiDriveResult:
    """Execute consecutive `read` / `data_query` runs and advance the interpreter.

    `materialized_tables` = a PROVIDER returning the interpreter's foreach `into` tables (accumulated
    rows from iterating a collection). It's folded into a data_query's source so a query AFTER a
    foreach can analyze the whole collected set. It MUST be called fresh right before each data_query
    (not snapshotted at entry): a foreach's `into` table is populated DURING this drain loop — when the
    last body read completes the interpreter resumes, accumulates, and yields the data_query — so a
    value captured at entry is still empty (regression 20260622_215814: the query saw no table though
    foreach had read all rows)."""
    cur_run = current_run
    failure_evidence: str | None = None  # last re-plannable non-UI failure (for Feasibility Guard kick-back)
    obs = done_observation
    frame = getattr(obs, "png_bytes", None) if obs is not None else None
    tables = getattr(obs, "tables", None) if obs is not None else None
    obs_url = observation_url

    def ensure_observation() -> Observation:
        nonlocal obs, frame, tables, obs_url
        if obs is None:
            obs_url = f"screenshot_read_{run_index}.png"
            obs = bundle.make_perception(platform, log_dir / obs_url).observe()
            frame = getattr(obs, "png_bytes", None)
            tables = getattr(obs, "tables", None)
        return obs

    while cur_run is not None and cur_run.kind in {"read", "data_query"}:
        run_for_turn = cur_run
        turn_started = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()
        context_reports: list[dict] = []
        reads: dict[str, str] = {}
        rows: list[dict[str, str]] = []
        completed = True
        summary = f"读取 {'、'.join(cur_run.returns) or cur_run.name}"
        executed_sql = cur_run.sql
        if cur_run.kind == "read" and cur_run.returns and getattr(cur_run, "list_read", False):
            from gui_agent.core.orchestrator.structured_read import structured_read_rows

            if frame is None:
                ensure_observation()
            rows = structured_read_rows(
                frame,
                cur_run.returns,
                read_spec=cur_run.read_spec,
                check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                context_reports=context_reports,
            )
            summary = f"列表读取 {len(rows)} 行（{'、'.join(cur_run.returns)}）"
            say(f"  [Orchestrator] 列表读取帧 {cur_run.returns} → {len(rows)} 行")
        elif cur_run.kind == "read" and cur_run.returns:
            from gui_agent.core.orchestrator.structured_read import structured_read

            if frame is None:
                ensure_observation()
            reads = structured_read(
                frame,
                cur_run.returns,
                read_spec=cur_run.read_spec,
                check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                context_reports=context_reports,
            )
            say(f"  [Orchestrator] 只读验收帧 {cur_run.returns} → {reads}")
        elif cur_run.kind == "data_query":
            from gui_agent.core.orchestrator.data_query import DataQueryError, execute_data_query
            from gui_agent.core.orchestrator.data_query_repair import repair_data_query_sql

            ensure_observation()
            query_tables = tables
            if getattr(cur_run, "data_scope", "complete") != "current":
                read_complete = getattr(platform, "read_complete_tables", None)
                if read_complete is not None:
                    try:
                        query_tables = read_complete() or query_tables
                    except Exception:
                        query_tables = tables
            # Fold in foreach-accumulated tables (e.g. per-review rows collected by iterating a list):
            # they're a complete, in-memory data source the query references by its `into` var name.
            # Pulled FRESH here (not at entry) so the just-completed foreach's into table is included.
            mats = materialized_tables() if callable(materialized_tables) else (materialized_tables or [])
            if mats:
                query_tables = list(query_tables or []) + list(mats)

            def _try_repair(reason: str) -> _RepairAttempt | None:
                repair = repair_data_query_sql(
                    goal=context.goal or "",
                    run_name=cur_run.name,
                    requested_returns=list(cur_run.returns),
                    original_sql=cur_run.sql,
                    tables=query_tables,
                    failure=reason,
                    recent_ui_context=_recent_ui_context(context),
                )
                if repair is None:
                    return None
                if not getattr(repair, "source_ok", True):
                    issue = getattr(repair, "source_issue", "") or getattr(repair, "reason", "")
                    return _RepairAttempt(
                        reads=None,
                        sql="",
                        reason=getattr(repair, "reason", "") or issue,
                        source_issue=issue or "当前已采集表格与任务要求的数据源口径不一致，需要回到界面修正后再查询",
                    )
                repaired_reads = execute_data_query(
                    query_tables,
                    repair.sql,
                    cur_run.returns,
                    require_complete=getattr(cur_run, "data_scope", "complete") != "current",
                )
                return _RepairAttempt(
                    reads=repaired_reads,
                    sql=repair.sql,
                    reason=repair.reason,
                )

            try:
                reads = execute_data_query(
                    query_tables,
                    cur_run.sql,
                    cur_run.returns,
                    require_complete=getattr(cur_run, "data_scope", "complete") != "current",
                )
                if _empty_query_result(reads, cur_run.returns) and _has_query_rows(query_tables):
                    repair_result = _try_repair(f"原 SQL 在非空表格上返回空结果: {reads}")
                    if repair_result is not None:
                        if repair_result.source_issue:
                            completed = False
                            summary = f"数据源与任务意图不一致: {repair_result.source_issue}"
                            say(f"  [Orchestrator] 数据查询失败：{summary}")
                        elif repair_result.reads is not None and not _empty_query_result(repair_result.reads, cur_run.returns):
                            reads = repair_result.reads
                            executed_sql = repair_result.sql
                            say(f"  [Orchestrator] 数据查询运行时修复：{repair_result.reason}")
                if completed:
                    summary = f"数据查询 {'、'.join(cur_run.returns) or cur_run.name}"
                    say(f"  [Orchestrator] 数据查询 {cur_run.returns} → {reads}")
            except DataQueryError as exc:
                repair_result = None
                repair_error: str | None = None
                if _has_query_rows(query_tables):
                    try:
                        repair_result = _try_repair(str(exc))
                    except DataQueryError as repair_exc:
                        repair_error = str(repair_exc)
                        repair_result = None
                if repair_result is not None and repair_result.source_issue:
                    completed = False
                    summary = f"数据源与任务意图不一致: {repair_result.source_issue}"
                    say(f"  [Orchestrator] 数据查询失败：{summary}")
                elif (
                    repair_result is not None
                    and repair_result.reads is not None
                    and not _empty_query_result(repair_result.reads, cur_run.returns)
                ):
                    reads = repair_result.reads
                    executed_sql = repair_result.sql
                    summary = f"数据查询 {'、'.join(cur_run.returns) or cur_run.name}"
                    say(f"  [Orchestrator] 数据查询运行时修复：{repair_result.reason}")
                    say(f"  [Orchestrator] 数据查询 {cur_run.returns} → {reads}")
                else:
                    completed = False
                    if repair_result is not None and repair_result.reads is not None:
                        summary = f"SQL 修复后仍返回空结果: {repair_result.reads}"
                    elif repair_error:
                        summary = f"{exc}; SQL 修复也失败: {repair_error}"
                    else:
                        summary = str(exc)
                    say(f"  [Orchestrator] 数据查询失败：{exc}")
        # A data_query that failed because its data source is empty / mismatched with the task is
        # RE-PLANNABLE: capture it so the loop can kick back to the orchestrator (re-decompose) rather
        # than end the run. (read failures are not routed — they're per-frame, not a plan-shape issue.)
        if not completed and run_for_turn.kind == "data_query":
            failure_evidence = summary
        result = package_result(
            run_for_turn,
            completed=completed,
            summary=summary,
            notes=[],
            reads=reads,
            rows=rows,
        )
        milestone_id = run_for_turn.var or f"m{run_index}_{run_for_turn.kind}"
        if not any(m.get("id") == milestone_id for m in context.milestones):
            context.milestones.append(
                {
                    "id": milestone_id,
                    "name": run_for_turn.name,
                    "description": run_for_turn.name,
                    "kind": run_for_turn.kind,
                    "success_condition": summary,
                }
            )
        context.turns.append(
            make_non_ui_turn(
                index=len(context.turns) + 1,
                observation_source=getattr(obs, "source", "non_ui") if obs is not None else "non_ui",
                milestone_id=milestone_id,
                summary=summary,
                kind=run_for_turn.kind,
                name=run_for_turn.name,
                var=run_for_turn.var or "",
                returns=list(run_for_turn.returns),
                read_spec=run_for_turn.read_spec,
                sql=executed_sql,
                data_scope=getattr(run_for_turn, "data_scope", "complete"),
                reads=dict(reads),
                completed=completed,
                observation_url=obs_url or "",
                started_at=turn_started,
                llm_calls=get_llm_call_count() - calls_before,
                input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                llm_context=context_reports,
            )
        )
        try:
            cur_run = interpreter_steps.send(result)
        except StopIteration as exc:
            return NonUiDriveResult(
                current_run=None,
                run_index=run_index,
                notes_mark=notes_mark,
                reply=exc.value or "",
                failure_evidence=failure_evidence,
            )
        run_index += 1
        save_context()

    if cur_run is not None:
        milestone = to_milestone(cur_run, run_index)
        supervisor.reseed(
            milestone,
            task_type=task_type_for(cur_run),
            fresh_advance=done_observation is not None,
        )
        if not any(m.get("id") == milestone.id for m in context.milestones):
            context.milestones.append(
                {
                    "id": milestone.id,
                    "name": milestone.name,
                    "description": milestone.description,
                    "kind": milestone.kind,
                    "success_condition": milestone.success_condition,
                }
            )
        notes_mark = len(context.content_notes)
    return NonUiDriveResult(
        current_run=cur_run,
        run_index=run_index,
        notes_mark=notes_mark,
        reply=None,
    )


def _empty_query_result(reads: dict[str, str], returns: list[str]) -> bool:
    if not reads:
        return True
    fields = returns or list(reads)
    values = [str(reads.get(field, "")).strip().lower() for field in fields]
    return bool(values) and all(value in {"", "[]", "{}", "null", "none"} for value in values)


def _has_query_rows(tables: list[dict[str, Any]] | None) -> bool:
    for table in tables or []:
        if isinstance(table, dict) and table.get("rows"):
            return True
    return False


def _recent_ui_context(context: PolicyContext, *, limit: int = 6) -> str:
    lines: list[str] = []
    for turn in (context.turns or [])[-limit:]:
        supervisor = getattr(turn, "supervisor", None)
        if supervisor is not None:
            summary = getattr(supervisor, "summary", "") or ""
            if summary:
                lines.append(summary)
        checker = getattr(turn, "checker", None)
        if checker is not None:
            reason = getattr(checker, "reason", "") or ""
            summary = getattr(checker, "summary", "") or ""
            if reason:
                lines.append(reason)
            elif summary:
                lines.append(summary)
    return "\n".join(lines[-limit:])
