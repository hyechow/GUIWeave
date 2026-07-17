"""Dispatch consecutive statements that complete without an interactive turn loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.program import Query, Read, Run, RunLike
from gui_agent.core.run.interactive import statement_id_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.schemas import Observation, PolicyContext

from .navigation import can_execute_navigation_immediately, execute_direct_navigation
from .observation import ObservationCursor
from .query import execute_query
from .read import execute_read
from .recording import record_statement_outcome


@dataclass
class ImmediateDispatchResult:
    """Observation and failure details produced while draining immediate statements."""

    reply: str | None = None
    observation: Observation | None = None
    observation_url: str | None = None
    failure_evidence: str | None = None


def is_immediate_statement(
    statement: RunLike | None,
    platform: Any,
    *,
    allow_navigation: bool = True,
) -> bool:
    """Whether a statement can complete now without entering the StatementContract loop."""
    return bool(
        statement is not None
        and (
            statement.is_query
            or (
                allow_navigation
                and can_execute_navigation_immediately(statement, platform)
            )
        )
    )


def drain_immediate_statements(
    *,
    program_runtime: ProgramRuntime,
    bundle: Any,
    platform: Any,
    log_dir: Path,
    check_knowledge: str,
    context: PolicyContext,
    save_context: Callable[[], None],
    say: Callable[[str], None],
    status: Callable[[str], None] | None = None,
    observation: Observation | None = None,
    observation_url: str | None = None,
    materialized_tables: "Callable[[], list[dict[str, Any]]] | None" = None,
    allow_navigation: bool = True,
) -> ImmediateDispatchResult:
    """Execute immediate statements and stop at the next interactive Run.

    ProgramRuntime remains the only component allowed to resume the interpreter.
    Individual executors process exactly one statement and know nothing about what follows.
    """
    statement = program_runtime.current
    failure_evidence: str | None = None
    cursor = ObservationCursor(
        bundle=bundle,
        platform=platform,
        log_dir=log_dir,
        observation=observation,
        observation_url=observation_url,
    )
    navigation_sequence = 0
    return_stack: list[str] = []

    def emit_status(message: str) -> None:
        if status is not None:
            status(message)

    while is_immediate_statement(
        statement,
        platform,
        allow_navigation=allow_navigation,
    ):
        assert statement is not None
        statement_id = statement_id_for_run(statement, program_runtime.index)
        instance_id = program_runtime.next_instance_id(statement_id)
        started_at = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()

        if isinstance(statement, Read):
            outcome = execute_read(
                statement,
                statement_index=program_runtime.index,
                cursor=cursor,
                bundle=bundle,
                platform=platform,
                log_dir=log_dir,
                check_knowledge=check_knowledge,
                say=say,
                status=emit_status,
            )
        elif isinstance(statement, Query):
            outcome = execute_query(
                statement,
                statement_index=program_runtime.index,
                cursor=cursor,
                context=context,
                materialized_tables=materialized_tables,
                say=say,
                status=emit_status,
            )
        else:
            assert isinstance(statement, Run)
            navigation_sequence += 1
            outcome = execute_direct_navigation(
                statement,
                statement_index=program_runtime.index,
                sequence=navigation_sequence,
                return_stack=return_stack,
                cursor=cursor,
                bundle=bundle,
                platform=platform,
                log_dir=log_dir,
                check_knowledge=check_knowledge,
                say=say,
                status=emit_status,
            )

        record_statement_outcome(
            statement,
            outcome,
            statement_index=program_runtime.index,
            context=context,
            program_runtime=program_runtime,
            started_at=started_at,
            llm_calls_before=calls_before,
            tokens_before=tokens_before,
            statement_instance_id=instance_id,
        )
        # The terminal fact must be durable before advancing the interpreter.
        save_context()
        if outcome.failure_evidence:
            failure_evidence = outcome.failure_evidence

        statement = program_runtime.send_outcome(outcome)
        if program_runtime.finished:
            if program_runtime.interpreter.run_log:
                terminal = program_runtime.interpreter.run_log[-1].result
                failure_evidence = failure_evidence or terminal.failure_evidence
            return ImmediateDispatchResult(
                reply=program_runtime.reply or "",
                observation=cursor.observation,
                observation_url=cursor.observation_url,
                failure_evidence=failure_evidence,
            )
        save_context()

    return ImmediateDispatchResult(
        observation=cursor.observation,
        observation_url=cursor.observation_url,
        failure_evidence=failure_evidence,
    )
