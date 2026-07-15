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
from gui_agent.core.run.context import extract_checker, extract_plan, extract_replan
from gui_agent.core.run.result import print_timings, print_turn_stats
from gui_agent.core.schemas import (
    AtomicRole,
    PolicyContext,
    PolicyTurn,
    ProgressTraceInfo,
    RuntimeConstraintInfo,
    StatementRuntimeSnapshot,
    SupervisorStep,
    TargetBinding,
)

MODEL_KEYS = (
    "supervisor",
    "supervisor.decompose",
    "action_policy",
    "reader",
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
    destroyed. Volatile screenshot/acquisition caches are deliberately restarted on resume.
    """
    rt = getattr(supervisor, "_statement_rt", None)
    if rt is None:
        return None
    monitor = rt.monitor
    return StatementRuntimeSnapshot(
        contract=rt.contract,
        retry_count=rt.retry_count,
        early_feasibility_probed=rt.early_feasibility_probed,
        scroll_count=rt.scroll_count,
        execution_scope=rt.execution_scope,
        last_page_identity=rt.last_page_identity,
        skip_initial_check=rt.skip_initial_check,
        statement_info_emitted=rt.statement_info_emitted,
        task_type=rt.task_type,
        constraints=[
            RuntimeConstraintInfo(
                text=entry.text,
                scope=entry.scope,
                source=entry.source,
            )
            for entry in rt.constraint_ledger.entries
        ],
        progress_trace=[
            ProgressTraceInfo(
                index=trace.index,
                state=trace.state,
                decision=trace.decision,
                interaction_state=trace.interaction_state,
                scope=trace.scope,
            )
            for trace in monitor.turns
        ],
        progress_values=list(monitor._progress_values),
        last_url=monitor._last_url,
        last_dom_state=monitor._last_dom_state,
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
    checker: dict | None = None,
    planner: dict | None = None,
    replan: dict | None = None,
    executed: bool = False,
    action_role: AtomicRole | None = None,
    action_key: str = "",
    suppressed_reason: str = "",
    binding: TargetBinding | None = None,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    read_added_content: bool = False,
    read_note_hash: str | None = None,
    timings: dict[str, float] | None = None,
    token_usage: dict[str, dict[str, int]] | None = None,
    sections_loaded: list[str] | None = None,
    llm_context: list[dict] | None = None,
    statement: Any = None,
    statement_instance_id: str = "",
    runtime_state: StatementRuntimeSnapshot | None = None,
) -> PolicyTurn:
    """Build a normal UI turn."""
    role = action_role or supervisor_step.atomic_role
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
        supervisor=supervisor_step.model_copy(update={"effect_signal": None}),
        action_decision=action_decision,
        checker=checker,
        planner=planner,
        replan=replan,
        executed=executed,
        action_signal=action_signal,
        effect_signal=supervisor_step.effect_signal,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        read_added_content=read_added_content,
        read_note_hash=read_note_hash,
        timings=timings or {},
        token_usage=token_usage or {},
        sections_loaded=list(sections_loaded or []),
        llm_context=list(llm_context or []),
        statement=statement,
        statement_instance_id=statement_instance_id,
        runtime_state=runtime_state,
    )


def make_verdict_turn(
    *,
    index: int,
    observation_source: str,
    observation_url: str = "",
    surface_id: str = "",
    supervisor_step: SupervisorStep,
    supervisor: Any,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_context: list[dict] | None = None,
    observation_only: bool = False,
    statement: Any = None,
    statement_instance_id: str = "",
    outcome_override: Any = None,
) -> PolicyTurn:
    """Build an action-less UI verdict turn from the current supervisor state.

    ``outcome_override`` replaces the raw ``supervisor_step.outcome`` on the recorded turn —
    used by the terminal observation path to persist the FILLED outcome (reads/rows added by
    the loop) rather than the raw native outcome.
    """
    turn = make_interactive_turn(
        index=index,
        observation_source=observation_source,
        observation_url=observation_url,
        surface_id=surface_id,
        supervisor_step=supervisor_step,
        action_decision=None,
        checker=extract_checker(supervisor),
        planner=extract_plan(supervisor),
        replan=extract_replan(supervisor),
        executed=False,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timings=getattr(supervisor, "_timings", {}),
        token_usage=getattr(supervisor, "_token_usage", {}),
        sections_loaded=list(getattr(supervisor, "_last_sections_loaded", []) or []),
        llm_context=list(
            llm_context
            if llm_context is not None
            else getattr(supervisor, "_context_reports", []) or []
        ),
        statement=statement,
        statement_instance_id=statement_instance_id,
        runtime_state=snapshot_statement_runtime(supervisor),
    )
    if outcome_override is not None:
        turn.supervisor = turn.supervisor.model_copy(update={"outcome": outcome_override})
    if observation_only:
        turn.operation_mode = "observation"
        turn.action_signal = None
    return turn


def emit_statement_fields(supervisor: Any) -> tuple[Any, str]:
    """Return (StatementInfo | None, instance_id) for the turn being recorded.

    The StatementInfo is emitted ONCE per invocation — on the first turn that calls this —
    and the ``statement_info_emitted`` flag flips so subsequent turns (and the terminal
    observation turn) carry ``statement=None`` but the SAME ``instance_id``. MUST be called
    while the statement runtime is live (before ``end_statement``); returns ``(None, "")``
    when no statement is active.
    """
    rt = getattr(supervisor, "_statement_rt", None)
    if rt is None:
        return (None, "")
    instance_id = getattr(rt, "instance_id", "") or ""
    if getattr(rt, "statement_info_emitted", False):
        return (None, instance_id)
    info = getattr(rt, "statement_info", None)
    rt.statement_info_emitted = True
    return (info, instance_id)


class SupervisorTimingCarry:
    """Carry same-turn handoff supervisor timing/token data across step() calls."""

    def __init__(self) -> None:
        self.timings: dict[str, float] = {}
        self.order: list[str] = []
        self.token_usage: dict[str, dict[str, int]] = {}
        self.context_reports: list[dict] = []

    def __bool__(self) -> bool:
        return bool(self.timings or self.context_reports)

    def collect(self, supervisor: Any) -> None:
        """Accumulate the supervisor timing/token state from a completed handoff step."""
        self._add_timings(getattr(supervisor, "_timings", {}) or {})
        self._add_tokens(getattr(supervisor, "_token_usage", {}) or {})
        self.context_reports.extend(list(getattr(supervisor, "_context_reports", []) or []))

    def merge_into(self, supervisor: Any) -> None:
        """Merge carried handoff timings into the supervisor's final step state."""
        if not self.timings and not self.context_reports:
            return
        self._add_timings(getattr(supervisor, "_timings", {}) or {})
        self._add_tokens(getattr(supervisor, "_token_usage", {}) or {})
        self.context_reports.extend(list(getattr(supervisor, "_context_reports", []) or []))
        supervisor._timings = self.timings
        supervisor._timings_order = self.order
        supervisor._token_usage = self.token_usage
        supervisor._context_reports = self.context_reports

    def _add_timings(self, timings: dict[str, float]) -> None:
        for key, value in timings.items():
            if key not in self.timings:
                self.order.append(key)
            self.timings[key] = self.timings.get(key, 0) + value

    def _add_tokens(self, token_usage: dict[str, dict[str, int]]) -> None:
        for key, usage in token_usage.items():
            current = self.token_usage.setdefault(key, {"input": 0, "output": 0})
            current["input"] += (usage or {}).get("input", 0)
            current["output"] += (usage or {}).get("output", 0)


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
    read_added_content: bool,
    read_note_hash: str | None,
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
        checker=extract_checker(supervisor),
        planner=extract_plan(supervisor),
        replan=extract_replan(supervisor),
        executed=executed,
        action_role=action_role,
        action_key=action_key,
        suppressed_reason=suppressed_reason,
        binding=binding,
        llm_calls=get_llm_call_count() - llm_calls_before,
        input_tokens=tokens_after[0] - tokens_before[0],
        output_tokens=tokens_after[1] - tokens_before[1],
        read_added_content=read_added_content,
        read_note_hash=read_note_hash,
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
        if action is not None:
            entry["action_type"] = action.action_type
            entry["action_desc"] = action.description
        if action_decision.not_found_reason:
            entry["not_found"] = action_decision.not_found_reason
    return entry


def make_immediate_statement_turn(
    *,
    index: int,
    observation_source: str,
    statement_id: str,
    summary: str,
    kind: str,
    name: str,
    var: str = "",
    returns: list[str] | None = None,
    read_spec: str = "",
    sql: str = "",
    data_scope: str = "complete",
    reads: dict[str, str] | None = None,
    completed: bool = True,
    observation_url: str = "",
    started_at: float | None = None,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    llm_context: list[dict] | None = None,
    statement: Any = None,
    statement_instance_id: str = "",
    outcome: Any = None,
) -> PolicyTurn:
    """Record a statement completed without an interactive decision loop.

    The persisted ``operation_mode=non_interactive`` value is a compatibility/budget label: it
    means no supervisor turn was consumed, not that every statement is free of GUI side effects.
    """
    from gui_agent.core.schemas import StatementOutcome

    elapsed = max(0.0, time.perf_counter() - started_at) if started_at is not None else 0.0
    if outcome is None:
        outcome = (
            StatementOutcome.completed(summary, verification="confirmed", reads=dict(reads or {}))
            if completed
            else StatementOutcome.failed(summary, reads=dict(reads or {}))
        )
    elif getattr(outcome, "observation", None) is not None:
        # The persisted turn keeps ``observation_url`` (a file path) as its screenshot
        # reference. Raw ``png_bytes`` must never enter the saved context: pydantic
        # json-dumps ``bytes`` as UTF-8, so non-text bytes (a PNG) raise UnicodeDecodeError
        # on save_context.model_dump(mode="json"). Strip the live observation here; nothing
        # downstream reads persisted-outcome bytes (only observation_url / outcome.reads).
        outcome = outcome.model_copy(update={"observation": None})
    return PolicyTurn(
        index=index,
        operation_mode="non_interactive",
        observation_source=observation_source,
        observation_url=observation_url,
        statement=statement,
        statement_instance_id=statement_instance_id,
        supervisor=SupervisorStep(
            should_act=False,
            instruction=None,
            summary=summary,
            statement_id=statement_id,
            statement_kind=kind if kind in {"navigation", "filter", "action", "collection", "verification"} else "collection",
            completion_strategy="read_once",
            outcome=outcome,
        ),
        action_decision=None,
        non_ui={
            "kind": kind,
            "name": name,
            "var": var,
            "returns": list(returns or []),
            "read_spec": read_spec,
            "sql": sql,
            "data_scope": data_scope,
            "reads": dict(reads or {}),
            "summary": summary,
            "completed": completed,
            "failed": not completed,
            "observation_url": observation_url,
        },
        executed=completed,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        timings={kind: elapsed},
        llm_context=list(llm_context or []),
    )
