"""Drain consecutive Data and Command statements outside the GUI React loop."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.program import Command, Data
from gui_agent.core.run.interactive import statement_id
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.schemas import Observation, PolicyContext

from .command import execute_command
from .data import execute_data_statement
from .observation import ObservationCursor
from .recording import record_statement_outcome


@dataclass
class ImmediateDispatchResult:
    reply: str | None = None
    observation: Observation | None = None
    observation_url: str | None = None
    failure_evidence: str | None = None
    replan_directive: str | None = None


def is_immediate_statement(invocation, platform: Any, *, allow_navigation: bool = True) -> bool:
    del platform, allow_navigation
    return bool(
        invocation is not None
        and isinstance(invocation.statement, (Data, Command))
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
    materialized_tables=None,
    allow_navigation: bool = True,
) -> ImmediateDispatchResult:
    del materialized_tables, allow_navigation
    invocation = program_runtime.current
    failure_evidence: str | None = None
    replan_directive: str | None = None
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
        iid = program_runtime.next_instance_id(sid)
        started_at = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()

        if isinstance(invocation.statement, Data):
            emit_status(f"Data 数据处理中：{invocation.goal}")
            if cursor.observation is None:
                cursor.ensure(program_runtime.index)
            reports: list[dict] = []
            outcome = execute_data_statement(
                invocation,
                observation=cursor.observation,
                check_knowledge=check_knowledge,
                prepare_vision_prompt_png=getattr(bundle, "prepare_vision_prompt_png", None),
                context_reports=reports,
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
        replan_directive = outcome.kickback or replan_directive
        invocation = program_runtime.send_outcome(outcome)
        if program_runtime.finished:
            return ImmediateDispatchResult(
                reply=program_runtime.reply or "",
                observation=cursor.observation,
                observation_url=cursor.observation_url,
                failure_evidence=failure_evidence,
                replan_directive=replan_directive,
            )
        save_context()

    return ImmediateDispatchResult(
        observation=cursor.observation,
        observation_url=cursor.observation_url,
        failure_evidence=failure_evidence,
        replan_directive=replan_directive,
    )


__all__ = [
    "ImmediateDispatchResult",
    "drain_immediate_statements",
    "is_immediate_statement",
]
