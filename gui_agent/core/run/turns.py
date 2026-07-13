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
from gui_agent.core.run.action_ledger import effective_action_role, semantic_action_key
from gui_agent.core.run.context import extract_checker, extract_plan, extract_replan
from gui_agent.core.run.result import print_timings, print_turn_stats
from gui_agent.core.run.state import sync_milestone_states
from gui_agent.core.schemas import (
    ActionSignal,
    MutationReceipt,
    PolicyContext,
    PolicyTurn,
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
        for turn in context.turns
        if getattr(turn, "operation_mode", "interactive") == "interactive"
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
) -> PolicyTurn:
    """Build a normal UI turn."""
    action = action_decision.action if action_decision else None
    role = (
        effective_action_role(supervisor_step, action)
        if action is not None
        else supervisor_step.atomic_role
    )
    if not action_key and action is not None:
        action_key = semantic_action_key(supervisor_step, action)
    if executed:
        execution = "dispatched"
    elif action_decision is not None and not action_decision.not_found_reason and not suppressed_reason:
        execution = "dispatch_failed"
    else:
        execution = "not_attempted"
    action_signal = ActionSignal(
        action_key=action_key,
        role=role,
        surface_id=surface_id,
        target_control=supervisor_step.target_control,
        target_value=(
            str(getattr(action, "text", "") or "")
            if role == "write" and action is not None
            else ""
        ),
        mutation_receipt=(
            MutationReceipt(
                statement_id=authorization.statement_id,
                subject_ref=authorization.subject_ref,
                field=authorization.field,
                intended_value=authorization.desired_value,
                source=authorization.source,
            )
            if role == "write"
            and execution == "dispatched"
            and (authorization := supervisor_step.mutation_authorization) is not None
            else None
        ),
        binding=binding,
        execution=execution,
        suppressed_reason=suppressed_reason,
        evidence=([suppressed_reason] if suppressed_reason else []),
    ) if action_decision is not None or suppressed_reason else None
    return PolicyTurn(
        index=index,
        operation_mode="interactive",
        observation_source=observation_source,
        observation_url=observation_url,
        supervisor=supervisor_step,
        action_decision=action_decision,
        checker=checker,
        planner=planner,
        replan=replan,
        executed=executed,
        action_signal=action_signal,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        read_added_content=read_added_content,
        read_note_hash=read_note_hash,
        timings=timings or {},
        token_usage=token_usage or {},
        sections_loaded=list(sections_loaded or []),
        llm_context=list(llm_context or []),
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
) -> PolicyTurn:
    """Build an action-less UI verdict turn from the current supervisor state."""
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
    )
    if observation_only:
        turn.operation_mode = "observation"
        turn.action_signal = None
    return turn


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
    """Persist model, milestone, task type, and collection-scope metadata."""
    if not context.models:
        for key in MODEL_KEYS:
            try:
                context.models[key] = resolve_llm_config(key).model or ""
            except Exception:
                pass

    if program is None and not context.milestones and hasattr(supervisor, "_milestones"):
        context.milestones = [
            {
                "id": milestone.id,
                "name": milestone.name,
                "description": milestone.description,
                "kind": milestone.kind,
                "success_condition": milestone.success_condition,
            }
            for milestone in supervisor._milestones.values()
        ]

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
) -> PolicyTurn:
    """Append the UI turn, sync persisted state, and notify the optional callback."""
    tokens_after = get_llm_token_usage()
    turn = make_interactive_turn(
        index=len(context.turns) + 1,
        observation_source=observation_source,
        observation_url=observation_url,
        surface_id=surface_id,
        supervisor_step=supervisor_step,
        action_decision=action_decision,
        checker=extract_checker(supervisor),
        planner=extract_plan(supervisor),
        replan=extract_replan(supervisor),
        executed=executed,
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
    )
    print_timings(supervisor)
    context.turns.append(turn)
    sync_milestone_states(supervisor, context)
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
        if action_decision.not_found_reason:
            entry["not_found"] = action_decision.not_found_reason
    return entry


def make_immediate_statement_turn(
    *,
    index: int,
    observation_source: str,
    milestone_id: str,
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
) -> PolicyTurn:
    """Record a statement completed without a Milestone decision loop.

    The persisted ``operation_mode=non_interactive`` value is a compatibility/budget label: it
    means no supervisor turn was consumed, not that every statement is free of GUI side effects.
    """
    elapsed = max(0.0, time.perf_counter() - started_at) if started_at is not None else 0.0
    return PolicyTurn(
        index=index,
        operation_mode="non_interactive",
        observation_source=observation_source,
        observation_url=observation_url,
        supervisor=SupervisorStep(
            should_act=False,
            instruction=None,
            stop=False,
            goal_completed=False,
            summary=summary,
            milestone_id=milestone_id,
            milestone_kind="collection",
            completion_strategy="read_once",
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
