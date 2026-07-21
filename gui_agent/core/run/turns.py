"""PolicyTurn construction helpers.

The runner has multiple execution paths (normal UI actions, orchestrator
handoffs, and non-UI primitives). Keep the saved turn shape in one place so those
paths stay consistent without making loop.py carry every field detail.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.run.action_signals import build_action_signal
from gui_agent.core.run.context import (
    extract_transition,
)
from gui_agent.core.run.result import print_timings, print_turn_stats
from gui_agent.core.schemas import (
    AtomicRole,
    Observation,
    PolicyContext,
    PolicyTurn,
    StatementOutcome,
    StatementOutcomeEvent,
    StatementRuntimeSnapshot,
    SupervisorStep,
    TargetBinding,
)

MODEL_KEYS = (
    "supervisor",
    "supervisor.decompose",
    "observation",
    "action_policy",
    "output",
    "router",
    "recon.navigator",
)


def interactive_turn_count(context: PolicyContext) -> int:
    """Count UI decision/action turns; non-UI primitives do not consume UI budget."""
    return sum(
        1
        for turn in context.journal.turns
        if getattr(turn, "operation_mode", "interactive") == "interactive"
    )


def snapshot_statement_runtime(supervisor: Any) -> StatementRuntimeSnapshot | None:
    """Project the active logical runtime into a replay payload.

    This is captured inside the turn event, after the policy decision and before the runtime is
    destroyed. Decision memory itself is rebuilt from Journal turns.
    """
    rt = getattr(supervisor, "_statement_rt", None)
    if rt is None:
        return None
    return StatementRuntimeSnapshot(
        contract=rt.contract,
        execution_scope=rt.execution_scope,
        statement_info_emitted=rt.statement_info_emitted,
        task_type=rt.task_type,
        initial_filters=getattr(supervisor, "_initial_filters", None),
    )


def make_interactive_turn(
    *,
    index: int,
    observation_source: str,
    observation_url: str = "",
    surface_id: str = "",
    supervisor_step: SupervisorStep,
    action_decision: Any = None,
    transition: dict | None = None,
    executed: bool = False,
    action_role: AtomicRole | None = None,
    action_key: str = "",
    suppressed_reason: str = "",
    binding: TargetBinding | None = None,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    timings: dict[str, float] | None = None,
    token_usage: dict[str, dict[str, int]] | None = None,
    sections_loaded: list[str] | None = None,
    llm_context: list[dict] | None = None,
    statement: Any = None,
    statement_instance_id: str = "",
    runtime_state: StatementRuntimeSnapshot | None = None,
) -> PolicyTurn:
    """Build a normal UI turn."""
    intent = supervisor_step.action_intent
    role = action_role or (intent.role if intent is not None else "prepare")
    action_signal = build_action_signal(
        supervisor_step,
        action_decision,
        role=role,
        action_key=action_key,
        surface_id=surface_id,
        executed=executed,
        suppressed_reason=suppressed_reason,
        binding=binding,
    )
    return PolicyTurn(
        index=index,
        operation_mode="interactive",
        observation_source=observation_source,
        observation_url=observation_url,
        supervisor=supervisor_step,
        action_decision=action_decision,
        transition=transition,
        executed=executed,
        action_signal=action_signal,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timings=timings or {},
        token_usage=token_usage or {},
        sections_loaded=list(sections_loaded or []),
        llm_context=list(llm_context or []),
        statement=statement,
        statement_instance_id=statement_instance_id,
        runtime_state=runtime_state,
    )


def make_statement_outcome_event(
    *,
    after_turn: int,
    observation_source: str,
    observation_url: str = "",
    supervisor_step: SupervisorStep,
    supervisor: Any,
    outcome: StatementOutcome,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_context: list[dict] | None = None,
    statement: Any = None,
    statement_instance_id: str = "",
) -> StatementOutcomeEvent:
    """Build the terminal journal fact while statement-local runtime is live."""
    statement_id = str(
        supervisor_step.statement_id
        or getattr(statement, "id", "")
        or ""
    )
    if not statement_instance_id or not statement_id:
        raise ValueError(
            "StatementOutcomeEvent requires statement_instance_id and statement_id"
        )
    return StatementOutcomeEvent(
        after_turn=after_turn,
        observation_source=observation_source,
        observation_url=observation_url,
        statement=statement,
        statement_instance_id=statement_instance_id,
        statement_id=statement_id,
        execution_scope=supervisor_step.execution_scope,
        outcome=outcome,
        transition=extract_transition(supervisor),
        pre_existing=supervisor_step.pre_existing,
        collection_summary=supervisor_step.collection_summary,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timings=getattr(supervisor, "_timings", {}) or {},
        token_usage=getattr(supervisor, "_token_usage", {}) or {},
        sections_loaded=list(
            getattr(supervisor, "_last_sections_loaded", []) or []
        ),
        llm_context=list(
            llm_context
            if llm_context is not None
            else getattr(supervisor, "_context_reports", []) or []
        ),
    )


def emit_statement_fields(supervisor: Any) -> tuple[Any, str]:
    """Return (StatementInfo | None, instance_id) for the turn being recorded.

    The StatementInfo is emitted ONCE per invocation — on the first turn that calls this —
    and the ``statement_info_emitted`` flag flips so subsequent turns (and the terminal
    outcome event) carry ``statement=None`` but the SAME ``instance_id``. MUST be called
    while the statement runtime is live (before ``end_statement``); returns ``(None, "")``
    when no statement is active.
    """
    rt = getattr(supervisor, "_statement_rt", None)
    if rt is None:
        return (None, "")
    instance_id = getattr(rt, "instance_id", "") or ""
    if getattr(rt, "statement_info_emitted", False):
        return (None, instance_id)
    from gui_agent.core.run.interactive import statement_info_from_contract

    info = statement_info_from_contract(rt.contract)
    rt.statement_info_emitted = True
    return (info, instance_id)


def sync_turn_metadata(
    *,
    context: PolicyContext,
    supervisor,
    sv_step: SupervisorStep,
    program,
    say: Callable[[str], None],
) -> None:
    """Persist model, statement, task type, and collection-scope metadata."""
    if not context.models:
        for key in MODEL_KEYS:
            try:
                context.models[key] = resolve_llm_config(key).model or ""
            except Exception:
                pass

    if hasattr(supervisor, "task_type") and context.task_type is None:
        context.task_type = supervisor.task_type
        say(f"任务类型: {context.task_type}")

    if sv_step.collection_scope and sv_step.collection_scope != context.collection_scope:
        context.collection_scope = sv_step.collection_scope
        scope = json.dumps(context.collection_scope.model_dump(exclude_none=True), ensure_ascii=False)
        say("采集范围: " + scope)


def record_interactive_turn(
    *,
    context: PolicyContext,
    observation_source: str,
    observation_url: str = "",
    surface_id: str = "",
    supervisor_step: SupervisorStep,
    supervisor: Any,
    action_decision: Any,
    executed: bool,
    action_role: AtomicRole | None = None,
    action_key: str = "",
    suppressed_reason: str = "",
    binding: TargetBinding | None = None,
    llm_calls_before: int,
    tokens_before: tuple[int, int],
    turn_started_at: float,
    save_context: Callable[[], None],
    silent: bool,
    on_turn: Any = None,
    statement: Any = None,
    statement_instance_id: str = "",
) -> PolicyTurn:
    """Append the UI turn, sync persisted state, and notify the optional callback."""
    tokens_after = get_llm_token_usage()
    turn = make_interactive_turn(
        index=len(context.journal.turns) + 1,
        observation_source=observation_source,
        observation_url=observation_url,
        surface_id=surface_id,
        supervisor_step=supervisor_step,
        action_decision=action_decision,
        transition=extract_transition(supervisor),
        executed=executed,
        action_role=action_role,
        action_key=action_key,
        suppressed_reason=suppressed_reason,
        binding=binding,
        llm_calls=get_llm_call_count() - llm_calls_before,
        input_tokens=tokens_after[0] - tokens_before[0],
        output_tokens=tokens_after[1] - tokens_before[1],
        timings=getattr(supervisor, "_timings", {}),
        token_usage=getattr(supervisor, "_token_usage", {}),
        sections_loaded=list(getattr(supervisor, "_last_sections_loaded", []) or []),
        llm_context=list(getattr(supervisor, "_context_reports", []) or []),
        statement=statement,
        statement_instance_id=statement_instance_id,
        runtime_state=snapshot_statement_runtime(supervisor),
    )
    print_timings(supervisor)
    context.journal.append_turn(turn)
    save_context()
    if not silent:
        print_turn_stats(turn.index, turn_started_at, llm_calls_before)
    if on_turn and callable(on_turn):
        on_turn(turn_callback_entry(turn, supervisor_step, action_decision, executed))
    return turn


def turn_callback_entry(
    turn,
    supervisor_step: SupervisorStep,
    action_decision: Any,
    executed: bool,
) -> dict:
    """Build the compact callback payload emitted after a turn is recorded."""
    entry: dict = {"no": turn.index, "summary": supervisor_step.summary, "executed": executed}
    if action_decision:
        action = action_decision.action
        entry["action_type"] = action.action_type
        entry["action_desc"] = action.description
    return entry


def make_immediate_statement_turn(
    *,
    index: int,
    observation_source: str,
    statement_id: str,
    summary: str,
    executor: str,
    goal: str,
    bind: str = "",
    outputs: dict[str, Any] | None = None,
    evidence: list[str] | None = None,
    observation_url: str = "",
    started_at: float | None = None,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_context: list[dict] | None = None,
    statement: Any = None,
    statement_instance_id: str = "",
    executed: bool = True,
) -> PolicyTurn:
    """Record execution of a primitive outside the interactive decision loop.

    Terminal status is recorded separately as ``StatementOutcomeEvent``. This
    turn keeps only the primitive execution facts and does not mirror completed
    or failed booleans.
    """
    elapsed = max(0.0, time.perf_counter() - started_at) if started_at is not None else 0.0
    return PolicyTurn(
        index=index,
        operation_mode="non_interactive",
        observation_source=observation_source,
        observation_url=observation_url,
        statement=statement,
        statement_instance_id=statement_instance_id,
        supervisor=SupervisorStep(
            summary=summary,
            statement_id=statement_id,
        ),
        action_decision=None,
        non_ui={
            "executor": executor,
            "goal": goal,
            "bind": bind,
            "outputs": dict(outputs or {}),
            "evidence": list(evidence or []),
            "summary": summary,
            "observation_url": observation_url,
        },
        executed=executed,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timings={executor: elapsed},
        llm_context=list(llm_context or []),
    )
