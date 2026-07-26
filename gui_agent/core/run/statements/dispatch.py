"""Drain non-Interact Program nodes outside the GUI React loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.run.contracts import Acquire, Command, Read
from gui_agent.core.run.interactive import statement_id
from gui_agent.core.schemas import Observation, PolicyContext

from .command import execute_command
from .acquire import execute_acquire_statement
from .binding import execute_read
from .observation import ObservationCursor
from .recording import record_statement_outcome


@dataclass
class ImmediateDispatchResult:
    reply: str | None = None
    observation: Observation | None = None
    observation_url: str | None = None
    failure_evidence: str | None = None


def is_immediate_statement(invocation, platform: Any, *, allow_navigation: bool = True) -> bool:
    del platform, allow_navigation
    return bool(
        invocation is not None
        and isinstance(invocation.statement, (Acquire, Read, Command))
    )


def drain_immediate_statements(
    *,
    program_runtime: Any,
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
    materialized_tables=None,
    allow_navigation: bool = True,
) -> ImmediateDispatchResult:
    del materialized_tables, allow_navigation
    invocation = program_runtime.current
    failure_evidence: str | None = None
    cursor = ObservationCursor(
        bundle=bundle,
        platform=platform,
        log_dir=log_dir,
        observation=observation,
        observation_url=observation_url,
    )

    def emit_status(message: str) -> None:
        if status:
            status(message)

    while is_immediate_statement(invocation, platform):
        assert invocation is not None
        sid = statement_id(invocation, program_runtime.index)
        iid = program_runtime.current_instance_id or program_runtime.next_instance_id(sid)
        started_at = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()

        if isinstance(invocation.statement, Acquire):
            emit_status(f"Acquire 集合采集中：{invocation.goal}")
            outcome = execute_acquire_statement(
                invocation,
                cursor=cursor,
                bundle=bundle,
                platform=platform,
                context=context,
                instance_id=iid,
                save_context=save_context,
                say=say,
                status=emit_status,
                check_knowledge=check_knowledge,
            )
        elif isinstance(invocation.statement, Read):
            emit_status(f"Read 观察绑定中：{invocation.goal}")
            if cursor.observation is None:
                cursor.ensure(program_runtime.index)
            outcome = execute_read(
                invocation,
                observation=cursor.observation,
                check_knowledge=check_knowledge,
                prepare_vision_prompt_png=getattr(bundle, "prepare_vision_prompt_png", None),
                say=say,
                status=emit_status,
            )
        else:
            outcome = execute_command(
                invocation,
                statement_index=program_runtime.index,
                cursor=cursor,
                platform=platform,
                status=emit_status,
                say=say,
            )

        outcome = program_runtime.adapt_outcome(outcome)
        record_statement_outcome(
            invocation,
            outcome,
            statement_index=program_runtime.index,
            context=context,
            program_runtime=program_runtime,
            started_at=started_at,
            llm_calls_before=calls_before,
            tokens_before=tokens_before,
            statement_instance_id=iid,
            observation_url=cursor.observation_url,
        )
        save_context()
        failure_evidence = outcome.failure_evidence or failure_evidence
        invocation = program_runtime.send_outcome(outcome)
        if program_runtime.finished:
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


__all__ = [
    "ImmediateDispatchResult",
    "drain_immediate_statements",
    "is_immediate_statement",
]
