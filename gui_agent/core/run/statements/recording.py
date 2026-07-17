"""Journal/report recording for immediate semantic statements."""

from __future__ import annotations

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.interactive import statement_id, statement_info
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.turns import make_immediate_statement_turn
from gui_agent.core.schemas import PolicyContext, StatementOutcome, StatementOutcomeEvent


def record_statement_outcome(
    invocation: StatementInvocation,
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
    for notice in outcome.recovery_notices:
        program_runtime.record_recovery(
            notice.cls,
            notice.mechanism,
            notice.site,
            detail=notice.detail,
            outcome=notice.outcome,
        )

    sid = statement_id(invocation, statement_index)
    info = statement_info(invocation, statement_index)
    iid = statement_instance_id or f"imm-{statement_index}:{sid}"
    observation = outcome.observation
    calls_after = get_llm_call_count()
    tokens_after = get_llm_token_usage()
    context.journal.append_turn(
        make_immediate_statement_turn(
            index=len(context.journal.turns) + 1,
            observation_source=(
                getattr(observation, "source", "non_ui")
                if observation is not None
                else "non_ui"
            ),
            statement_id=sid,
            summary=outcome.summary,
            executor=invocation.executor,
            goal=invocation.goal,
            bind=invocation.bind or "",
            outputs=dict(outcome.outputs),
            evidence=list(outcome.evidence),
            observation_url=outcome.observation_url or "",
            started_at=started_at,
            llm_calls=calls_after - llm_calls_before,
            input_tokens=tokens_after[0] - tokens_before[0],
            output_tokens=tokens_after[1] - tokens_before[1],
            llm_context=outcome.context_reports,
            statement=info,
            statement_instance_id=iid,
            executed=outcome.is_completed,
        )
    )
    context.journal.append_statement_outcome(
        StatementOutcomeEvent(
            after_turn=len(context.journal.turns),
            observation_source=(
                getattr(observation, "source", "non_ui")
                if observation is not None
                else "non_ui"
            ),
            observation_url=outcome.observation_url or "",
            statement_instance_id=iid,
            statement_id=sid,
            outcome=outcome,
        )
    )


__all__ = ["record_statement_outcome"]
