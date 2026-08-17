"""Own replacement decisions after a Worker execution path is disproved."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
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


class Strategy:
    """Decide replacement policy using the Worker model as an inference backend."""

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
        attempted = context.get("attempted_strategies") or []
        if replacement == original or replacement.model_dump(mode="json") in attempted:
            issues.append("replacement strategy has already been attempted")
        if approach_is_procedural(replacement.approach):
            issues.append(
                "replacement approach must be one source or implementation method, "
                "without an action, action argument, URL, or ordered GUI procedure"
            )
        return issues

    @classmethod
    def _replacement(
        cls,
        decision: dict[str, Any],
        original: WorkerStrategy,
        context: dict[str, Any],
    ) -> tuple[WorkerStrategy | None, list[str]]:
        choice, reason = decision.get("decision"), decision.get("reason")
        candidate = decision.get("strategy")
        if choice not in {"replace", "stop"} or not isinstance(reason, str) or not reason:
            raise ValueError("decision and non-empty reason are required")
        if choice == "stop":
            if candidate is not None:
                raise ValueError("stop forbids a strategy")
            return None, []
        if not isinstance(candidate, dict):
            raise ValueError("replace requires one complete strategy")
        replacement = WorkerStrategy.model_validate(candidate)
        return replacement, cls._issues(original, replacement, context)

    def decide(self, *, context: dict[str, Any], original_strategy: WorkerStrategy,
               on_event: Callable[..., None]
               ) -> tuple[WorkerStrategy | None, str]:
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
        replacement: WorkerStrategy | None = None
        diagnostics = []
        elapsed_s = 0.0
        response = None
        for attempt in range(2):
            started_at = time.perf_counter()
            response = model.invoke(messages)
            elapsed_s += time.perf_counter() - started_at
            try:
                decision = parse_json_object(response.content)
                replacement, diagnostics = self._replacement(
                    decision,
                    original_strategy,
                    context,
                )
                if not diagnostics:
                    break
            except Exception as exc:  # one bounded repair
                replacement = None
                diagnostics = [f"{type(exc).__name__}: {exc}"]
            if attempt == 0:
                messages.extend([
                    response,
                    HumanMessage(content=(
                        "Strategy decision repair: " + "; ".join(diagnostics)
                        + ". Return exactly one valid replace or stop decision."
                    )),
                ])

        assert response is not None
        selected = bool(decision.get("decision") == "replace" and replacement and not diagnostics)
        valid_stop = decision.get("decision") == "stop" and not diagnostics
        invalid_reason = "Strategy did not produce a valid replacement strategy."
        reason = str(decision.get("reason") or invalid_reason) if (
            selected or valid_stop
        ) else invalid_reason
        reports = diagnostic_prompt_reports("tool_agent.strategy_decide", messages, response,
            parsed=decision or {"diagnostics": diagnostics},
            schema="StrategyDecision",
        )
        on_event(
            "strategy_decision",
            decision="replace" if selected else "stop",
            reason=reason,
            strategy=replacement.model_dump(mode="json") if selected else None,
            diagnostics=diagnostics,
            llm_elapsed_s=round(elapsed_s, 3),
            token_usage=response_usage(response),
            context_reports=reports,
        )
        return (replacement if selected else None), reason
