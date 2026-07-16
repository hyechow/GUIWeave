"""Table-backed Query executor and its bounded SQL-repair path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gui_agent.core.orchestrator.program import Query
from gui_agent.core.schemas import PolicyContext

from .observation import ObservationCursor
from .outcome import RecoveryNotice, StatementOutcome


@dataclass
class _RepairAttempt:
    reads: dict[str, str] | None
    sql: str
    reason: str
    source_issue: str = ""


def _empty_query_result(reads: dict[str, str], returns: list[str]) -> bool:
    if not reads:
        return True
    fields = returns or list(reads)
    values = [str(reads.get(field, "")).strip().lower() for field in fields]
    return bool(values) and all(value in {"", "[]", "{}", "null", "none"} for value in values)


def _has_query_rows(tables: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(table, dict) and table.get("rows") for table in tables or [])


def _recent_ui_context(context: PolicyContext, *, limit: int = 6) -> str:
    lines: list[str] = []
    for turn in (context.journal.turns or [])[-limit:]:
        supervisor = getattr(turn, "supervisor", None)
        if supervisor is not None:
            summary = getattr(supervisor, "summary", "") or ""
            if summary:
                lines.append(summary)
        transition = getattr(turn, "transition", None) or {}
        proposal = transition.get("proposal") if isinstance(transition, dict) else None
        if isinstance(proposal, dict):
            reason = str(proposal.get("reason") or "")
            summary = str(proposal.get("summary") or "")
            if reason:
                lines.append(reason)
            elif summary:
                lines.append(summary)
    return "\n".join(lines[-limit:])


def _query_tables(
    cursor: ObservationCursor,
    materialized_tables: Callable[[], list[dict[str, Any]]] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    current = list(cursor.tables or [])
    produced = (
        materialized_tables()
        if callable(materialized_tables)
        else materialized_tables
    )
    materialized = list(produced or [])
    # Materialized foreach tables go first so their explicit aliases win over a partial DOM table.
    return list(materialized) + current


def execute_query(
    run: Query,
    *,
    statement_index: int,
    cursor: ObservationCursor,
    context: PolicyContext,
    materialized_tables: Callable[[], list[dict[str, Any]]] | list[dict[str, Any]] | None,
    say: Callable[[str], None],
    status: Callable[[str], None],
) -> StatementOutcome:
    """Execute one data query, applying the existing bounded repair policy when justified."""
    from gui_agent.core.orchestrator.primitives.data_query import DataQueryError, execute_data_query
    from gui_agent.core.orchestrator.primitives.data_query_repair import repair_data_query_sql

    status(f"数据查询 {'、'.join(run.returns) or run.name}")
    cursor.ensure(statement_index)
    tables = _query_tables(cursor, materialized_tables)
    reads: dict[str, str] = {}
    completed = True
    summary = f"数据查询 {'、'.join(run.returns) or run.name}"
    executed_sql = run.sql
    notices: list[RecoveryNotice] = []
    site = str(run.var or run.name)

    def record_repair(outcome: str, detail: str = "") -> None:
        notices.append(RecoveryNotice(
            cls="data_source_error",
            mechanism="sql_repair",
            site=site,
            detail=detail[:200],
            outcome=outcome,
        ))

    def try_repair(reason: str) -> _RepairAttempt | None:
        repair = repair_data_query_sql(
            goal=context.goal or "",
            run_name=run.name,
            requested_returns=list(run.returns),
            original_sql=run.sql,
            tables=tables,
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
                source_issue=(
                    issue
                    or "当前已采集表格与任务要求的数据源口径不一致，需要回到界面修正后再查询"
                ),
            )
        repaired_reads = execute_data_query(
            tables,
            repair.sql,
            run.returns,
            require_complete=run.data_scope != "current",
        )
        return _RepairAttempt(reads=repaired_reads, sql=repair.sql, reason=repair.reason)

    try:
        reads = execute_data_query(
            tables,
            run.sql,
            run.returns,
            require_complete=run.data_scope != "current",
        )
        if _empty_query_result(reads, run.returns) and _has_query_rows(tables):
            repair = try_repair(f"原 SQL 在非空表格上返回空结果: {reads}")
            if repair is not None:
                if repair.source_issue:
                    completed = False
                    summary = f"数据源与任务意图不一致: {repair.source_issue}"
                    record_repair("source_mismatch", repair.source_issue)
                    say(f"  [Orchestrator] 数据查询失败：{summary}")
                elif repair.reads is not None and not _empty_query_result(repair.reads, run.returns):
                    reads = repair.reads
                    executed_sql = repair.sql
                    record_repair("recovered", repair.reason)
                    say(f"  [Orchestrator] 数据查询运行时修复：{repair.reason}")
                else:
                    record_repair("no_improvement", repair.reason)
        if completed:
            say(f"  [Orchestrator] 数据查询 {run.returns} → {reads}")
    except DataQueryError as exc:
        repair = None
        repair_error: str | None = None
        if _has_query_rows(tables):
            try:
                repair = try_repair(str(exc))
            except DataQueryError as repair_exc:
                repair_error = str(repair_exc)

        if repair is not None and repair.source_issue:
            completed = False
            summary = f"数据源与任务意图不一致: {repair.source_issue}"
            record_repair("source_mismatch", repair.source_issue)
            say(f"  [Orchestrator] 数据查询失败：{summary}")
        elif (
            repair is not None
            and repair.reads is not None
            and not _empty_query_result(repair.reads, run.returns)
        ):
            reads = repair.reads
            executed_sql = repair.sql
            record_repair("recovered", repair.reason)
            say(f"  [Orchestrator] 数据查询运行时修复：{repair.reason}")
            say(f"  [Orchestrator] 数据查询 {run.returns} → {reads}")
        else:
            completed = False
            if repair is not None and repair.reads is not None:
                summary = f"SQL 修复后仍返回空结果: {repair.reads}"
                record_repair("no_improvement", summary)
            elif repair_error:
                summary = f"{exc}; SQL 修复也失败: {repair_error}"
                record_repair("repair_failed", repair_error)
            else:
                summary = str(exc)
            say(f"  [Orchestrator] 数据查询失败：{exc}")

    failure_evidence = summary if not completed else None
    if failure_evidence:
        notices.append(RecoveryNotice(
            cls="data_source_error",
            mechanism="data_query_failure",
            site=site,
            detail=failure_evidence[:200],
            outcome="replan_candidate",
        ))

    if completed:
        return StatementOutcome.completed(
            summary,
            verification="confirmed",
            reads=reads,
            observation=cursor.observation,
            observation_url=cursor.observation_url,
            executed_sql=executed_sql,
            recovery_notices=notices,
        )
    return StatementOutcome.failed(
        summary,
        reads=reads,
        observation=cursor.observation,
        observation_url=cursor.observation_url,
        executed_sql=executed_sql,
        recovery_notices=notices,
        failure_evidence=failure_evidence,
    )
