"""Reporting and recovery-ledger side effects for immediate statement outcomes."""

from __future__ import annotations

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.program import RunLike
from gui_agent.core.run.interactive import statement_id_for_run, statement_info_for_run
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.turns import make_immediate_statement_turn
from gui_agent.core.schemas import PolicyContext, StatementOutcomeEvent

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
    calls_after = get_llm_call_count()
    tokens_after_now = get_llm_token_usage()
    context.journal.append_turn(make_immediate_statement_turn(
        index=len(context.journal.turns) + 1,
        observation_source=(
            getattr(observation, "source", "non_ui")
            if observation is not None
            else "non_ui"
        ),
        statement_id=statement_id,
        summary=outcome.summary,
        kind=statement.kind,
        name=statement.name,
        var=statement.var or "",
        returns=list(statement.returns),
        read_spec=statement.read_spec,
        sql=outcome.executed_sql,
        data_scope=getattr(statement, "data_scope", "complete"),
        reads=dict(outcome.reads),
        observation_url=outcome.observation_url or "",
        started_at=started_at,
        llm_calls=calls_after - llm_calls_before,
        input_tokens=tokens_after_now[0] - tokens_before[0],
        output_tokens=tokens_after_now[1] - tokens_before[1],
        llm_context=outcome.context_reports,
        statement=info,
        statement_instance_id=iid,
        executed=outcome.is_completed,
    ))
    context.journal.append_statement_outcome(StatementOutcomeEvent(
        after_turn=len(context.journal.turns),
        observation_source=(
            getattr(observation, "source", "non_ui")
            if observation is not None
            else "non_ui"
        ),
        observation_url=outcome.observation_url or "",
        statement_instance_id=iid,
        statement_id=statement_id,
        statement_kind=(
            statement.kind
            if statement.kind
            in {"navigation", "filter", "action", "collection", "verification"}
            else "collection"
        ),
        outcome=outcome,
    ))
