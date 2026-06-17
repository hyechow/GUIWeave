"""PolicyTurn construction helpers.

The runner has multiple execution paths (normal UI actions, orchestrator
handoffs, and non-UI primitives). Keep the saved turn shape in one place so those
paths stay consistent without making loop.py carry every field detail.
"""

from __future__ import annotations

import time
from typing import Any

from gui_agent.core.run.context import extract_checker, extract_plan, extract_replan
from gui_agent.core.schemas import PolicyContext, PolicyTurn, SupervisorStep


def interactive_turn_count(context: PolicyContext) -> int:
    """Count UI decision/action turns; non-UI primitives do not consume UI budget."""
    return sum(
        1
        for turn in context.turns
        if getattr(turn, "operation_mode", "interactive") != "non_interactive"
    )


def make_interactive_turn(
    *,
    index: int,
    observation_source: str,
    supervisor_step: SupervisorStep,
    action_decision: Any = None,
    checker: dict | None = None,
    planner: dict | None = None,
    replan: dict | None = None,
    executed: bool = False,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    read_added_content: bool = False,
    read_note_hash: str | None = None,
    timings: dict[str, float] | None = None,
    token_usage: dict[str, dict[str, int]] | None = None,
    sections_loaded: list[str] | None = None,
) -> PolicyTurn:
    """Build a normal UI turn."""
    return PolicyTurn(
        index=index,
        operation_mode="interactive",
        observation_source=observation_source,
        supervisor=supervisor_step,
        action_decision=action_decision,
        checker=checker,
        planner=planner,
        replan=replan,
        executed=executed,
        llm_calls=llm_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        read_added_content=read_added_content,
        read_note_hash=read_note_hash,
        timings=timings or {},
        token_usage=token_usage or {},
        sections_loaded=list(sections_loaded or []),
    )


def make_verdict_turn(
    *,
    index: int,
    observation_source: str,
    supervisor_step: SupervisorStep,
    supervisor: Any,
    llm_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> PolicyTurn:
    """Build an action-less UI verdict turn from the current supervisor state."""
    return make_interactive_turn(
        index=index,
        observation_source=observation_source,
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
    )


class SupervisorTimingCarry:
    """Carry same-turn handoff supervisor timing/token data across step() calls."""

    def __init__(self) -> None:
        self.timings: dict[str, float] = {}
        self.order: list[str] = []
        self.token_usage: dict[str, dict[str, int]] = {}

    def __bool__(self) -> bool:
        return bool(self.timings)

    def collect(self, supervisor: Any) -> None:
        """Accumulate the supervisor timing/token state from a completed handoff step."""
        self._add_timings(getattr(supervisor, "_timings", {}) or {})
        self._add_tokens(getattr(supervisor, "_token_usage", {}) or {})

    def merge_into(self, supervisor: Any) -> None:
        """Merge carried handoff timings into the supervisor's final step state."""
        if not self.timings:
            return
        self._add_timings(getattr(supervisor, "_timings", {}) or {})
        self._add_tokens(getattr(supervisor, "_token_usage", {}) or {})
        supervisor._timings = self.timings
        supervisor._timings_order = self.order
        supervisor._token_usage = self.token_usage

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


def make_non_ui_turn(
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
) -> PolicyTurn:
    """Build a non-interactive primitive turn (`read` / `data_query`)."""
    elapsed = max(0.0, time.perf_counter() - started_at) if started_at is not None else 0.0
    return PolicyTurn(
        index=index,
        operation_mode="non_interactive",
        observation_source=observation_source,
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
    )
