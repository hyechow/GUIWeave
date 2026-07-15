"""Reporting and recovery-ledger side effects for immediate statement outcomes."""

from __future__ import annotations

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.program import RunLike
from gui_agent.core.run.interactive import statement_id_for_run, statement_info_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.turns import make_immediate_statement_turn
from gui_agent.core.schemas import PolicyContext

from .outcome import StatementOutcome


def record_statement_outcome(
    statement: RunLike,
    outcome: StatementOutcome,
    *,
    statement_index: int,
    context: PolicyContext,
    program_runtime: ProgramRuntime,
    started_at: float,
    llm_calls_before: int,
    tokens_before: tuple[int, int],
    statement_instance_id: str = "",
) -> None:
    """Apply reporting and ledger effects after an executor has finished one statement."""
    for notice in outcome.recovery_notices:
        program_runtime.record_recovery(
            notice.cls,
            notice.mechanism,
            notice.site,
            detail=notice.detail,
            outcome=notice.outcome,
        )

    statement_id = statement_id_for_run(statement, statement_index)
    info = statement_info_for_run(statement, statement_index)
    iid = statement_instance_id or f"imm-{statement_index}:{statement_id}"

    observation = outcome.observation
    context.journal.append(make_immediate_statement_turn(
        index=len(context.journal.events) + 1,
        observation_source=(
            getattr(observation, "source", "non_ui")
            if observation is not None
            else "non_ui"
        ),
        milestone_id=statement_id,
        summary=outcome.summary,
        kind=statement.kind,
        name=statement.name,
        var=statement.var or "",
        returns=list(statement.returns),
        read_spec=statement.read_spec,
        sql=outcome.executed_sql,
        data_scope=getattr(statement, "data_scope", "complete"),
        reads=dict(outcome.reads),
        completed=outcome.is_completed,
        observation_url=outcome.observation_url or "",
        started_at=started_at,
        llm_calls=get_llm_call_count() - llm_calls_before,
        input_tokens=get_llm_token_usage()[0] - tokens_before[0],
        output_tokens=get_llm_token_usage()[1] - tokens_before[1],
        llm_context=outcome.context_reports,
        statement=info,
        statement_instance_id=iid,
        outcome=outcome,
    ))
