"""Own replacement decisions after a Worker execution path is disproved."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from gui_agent.core.tool_agent.contracts import (
    WorkerOutcome,
    WorkerStrategy,
    approach_is_procedural,
)
from gui_agent.core.tool_agent.protocol import (
    cacheable_system_message,
    diagnostic_prompt_reports,
    parse_json_object,
    response_usage,
)
from gui_agent.prompts import load_prompt_text
from llm.provider_config import chat_request_kwargs


_STRATEGY_SYSTEM = load_prompt_text("task.tool_agent.strategy_decide")
_FAILURE_ROUTES = {
    "worker_blocked": "replace",
    "navigation_blocked": "replace",
    "action_contract_invalid": "replace",
    "platform_rejected": "replace",
}


ReflectionRoute = Literal[
    "resume", "reconcile_state", "revise_approach", "escalate_to_master", "stop"
]


@dataclass(frozen=True)
class ReflectionResult:
    decision: ReflectionRoute
    reason: str
    strategy: WorkerStrategy | None = None
    preserve_progress: bool = True
    invalidate: tuple[str, ...] = ()

class Reflector:
    """Diagnose a disproved execution path without making GUI decisions."""

    def __init__(self, generation_model: Any, *, generation_model_name: str = "",
                 explicit_cache: bool = False) -> None:
        self.generation_model = generation_model
        self.generation_model_name = generation_model_name
        self.explicit_cache = explicit_cache

    @classmethod
    def route(cls, outcome: WorkerOutcome) -> Literal[
        "complete", "replace", "master", "abort"
    ]:
        if outcome.phase == "completed":
            return "complete"
        return _FAILURE_ROUTES.get(outcome.failure_kind or "worker_blocked", "abort")

    @staticmethod
    def _issues(
        original: WorkerStrategy,
        replacement: WorkerStrategy,
        context: dict[str, Any],
    ) -> list[str]:
        issues = []
        attempted = (
            context.get("attempted_approaches")
            or context.get("attempted_strategies")
            or []
        )
        if replacement == original or replacement.model_dump(mode="json") in attempted:
            issues.append("replacement strategy has already been attempted")
        if approach_is_procedural(replacement.approach):
            issues.append(
                "replacement approach must be one source or implementation method, "
                "without an action, action argument, URL, or ordered GUI procedure"
            )
        return issues

    @classmethod
    def _reflection(
        cls,
        decision: dict[str, Any],
        original: WorkerStrategy,
        context: dict[str, Any],
    ) -> tuple[ReflectionResult, list[str]]:
        recommendation = decision.get("recommendation")
        diagnosis = decision.get("diagnosis")
        if isinstance(recommendation, dict):
            choice = recommendation.get("decision")
            reason = (diagnosis or {}).get("reason") if isinstance(diagnosis, dict) else ""
            approach = recommendation.get("approach")
            preserve = recommendation.get("preserve_progress", True)
            invalidate = recommendation.get("invalidate") or []
        else:  # historical replay compatibility
            legacy = decision.get("decision")
            choice = "revise_approach" if legacy == "replace" else legacy
            reason = decision.get("reason")
            candidate = decision.get("strategy")
            legacy_strategy = (
                WorkerStrategy.model_validate(candidate)
                if isinstance(candidate, dict) else None
            )
            approach = legacy_strategy.approach if legacy_strategy is not None else None
            preserve = True
            invalidate = []
        if choice not in {
            "resume", "reconcile_state", "revise_approach", "escalate_to_master", "stop",
        } or not isinstance(reason, str) or not reason:
            raise ValueError("typed recommendation and non-empty diagnosis reason are required")
        if preserve is not True:
            raise ValueError("Reflector must preserve reduced progress")
        if not isinstance(invalidate, list) or any(not isinstance(ref, str) for ref in invalidate):
            raise ValueError("invalidate must contain audit reference strings")
        replacement = None
        issues: list[str] = []
        if choice == "revise_approach":
            if not isinstance(approach, str) or not approach.strip():
                raise ValueError("revise_approach requires one complete approach")
            replacement = WorkerStrategy(approach=approach)
            issues = cls._issues(original, replacement, context)
        elif approach not in (None, ""):
            raise ValueError(f"{choice} forbids an approach")
        return ReflectionResult(
            decision=choice, reason=reason, strategy=replacement,
            preserve_progress=True, invalidate=tuple(invalidate),
        ), issues

    def reflect(self, *, context: dict[str, Any], original_strategy: WorkerStrategy,
                on_event: Callable[..., None]) -> ReflectionResult:
        messages: list[Any] = [
            cacheable_system_message(_STRATEGY_SYSTEM, enabled=self.explicit_cache),
            HumanMessage(content=json.dumps(context, ensure_ascii=False)),
        ]
        model = self.generation_model.bind(
            response_format={"type": "json_object"},
            max_tokens=600,
            **chat_request_kwargs(self.generation_model_name),
        )
        decision: dict[str, Any] = {}
        reflection: ReflectionResult | None = None
        diagnostics = []
        elapsed_s = 0.0
        response = None
        for attempt in range(2):
            started_at = time.perf_counter()
            response = model.invoke(messages)
            elapsed_s += time.perf_counter() - started_at
            try:
                decision = parse_json_object(response.content)
                reflection, diagnostics = self._reflection(
                    decision,
                    original_strategy,
                    context,
                )
                if not diagnostics:
                    break
            except Exception as exc:  # one bounded repair
                reflection = None
                diagnostics = [f"{type(exc).__name__}: {exc}"]
            if attempt == 0:
                messages.extend([
                    response,
                    HumanMessage(content=(
                        "Strategy decision repair: " + "; ".join(diagnostics)
                        + ". Return exactly one valid typed reflection recommendation."
                    )),
                ])

        assert response is not None
        valid = reflection is not None and not diagnostics
        invalid_reason = "Reflector did not produce a valid recommendation."
        if not valid:
            reflection = ReflectionResult(decision="stop", reason=invalid_reason)
        reports = diagnostic_prompt_reports("tool_agent.strategy_decide", messages, response,
            parsed=decision or {"diagnostics": diagnostics},
            schema="StrategyDecision",
        )
        on_event(
            "reflection_decision",
            decision=reflection.decision,
            reason=reflection.reason,
            strategy=(reflection.strategy.model_dump(mode="json")
                      if reflection.strategy is not None else None),
            preserve_progress=reflection.preserve_progress,
            invalidate=list(reflection.invalidate),
            diagnostics=diagnostics,
            llm_elapsed_s=round(elapsed_s, 3),
            token_usage=response_usage(response),
            context_reports=reports,
        )
        return reflection
