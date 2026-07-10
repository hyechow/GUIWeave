"""Dispatch consecutive statements that can complete without a Milestone loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.program import Query, Read, Run, RunLike
from gui_agent.core.schemas import Observation, PolicyContext

from .navigation import can_execute_navigation_immediately, execute_direct_navigation
from .observation import ObservationCursor
from .query import execute_query
from .read import execute_read
from .recording import record_statement_outcome


@dataclass
class ImmediateDispatchResult:
    """Interpreter cursor and live observation after draining immediate statements."""

    current_statement: RunLike | None
    statement_index: int
    reply: str | None = None
    observation: Observation | None = None
    observation_url: str | None = None
    failure_evidence: str | None = None


def is_immediate_statement(statement: RunLike | None, platform: Any) -> bool:
    """Whether a statement can complete now without entering the Milestone loop."""
    return bool(
        statement is not None
        and (
            statement.is_query
            or can_execute_navigation_immediately(statement, platform)
        )
    )


def drain_immediate_statements(
    *,
    current_statement: RunLike | None,
    statement_index: int,
    interpreter_steps: Any,
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
    recovery: Any = None,
) -> ImmediateDispatchResult:
    """Execute immediate statements and stop at the next Milestone-backed Run.

    This is the only immediate-runtime component allowed to resume the Program generator.
    Individual executors process exactly one statement and know nothing about what follows.
    """
    statement = current_statement
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

    while is_immediate_statement(statement, platform):
        assert statement is not None
        started_at = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()

        if isinstance(statement, Read):
            outcome = execute_read(
                statement,
                statement_index=statement_index,
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
                statement_index=statement_index,
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
                statement_index=statement_index,
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
            statement_index=statement_index,
            context=context,
            recovery=recovery,
            started_at=started_at,
            llm_calls_before=calls_before,
            tokens_before=tokens_before,
        )
        if outcome.failure_evidence:
            failure_evidence = outcome.failure_evidence

        try:
            statement = interpreter_steps.send(outcome.result)
        except StopIteration as exc:
            return ImmediateDispatchResult(
                current_statement=None,
                statement_index=statement_index,
                reply=exc.value or "",
                observation=cursor.observation,
                observation_url=cursor.observation_url,
                failure_evidence=failure_evidence,
            )
        statement_index += 1
        save_context()

    return ImmediateDispatchResult(
        current_statement=statement,
        statement_index=statement_index,
        observation=cursor.observation,
        observation_url=cursor.observation_url,
        failure_evidence=failure_evidence,
    )
