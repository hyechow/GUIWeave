"""Independent strategy search for one immutable logical Worker contract."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, model_validator

from gui_agent.core.tool_agent.contracts import StrictModel, WorkerSpec
from gui_agent.core.tool_agent.protocol import (
    cacheable_system_message,
    diagnostic_prompt_reports,
    parse_json_object,
    response_usage,
)
from gui_agent.prompts import load_prompt_text
from llm.provider_config import chat_request_kwargs


_PROPOSE_SYSTEM = load_prompt_text("task.tool_agent.strategy_propose")
_SELECT_SYSTEM = load_prompt_text("task.tool_agent.strategy_select")


def _ground_navigation_urls(
    candidate: "StrategyCandidate",
    context: dict[str, Any],
) -> tuple["StrategyCandidate", list[str]]:
    """Reduce unevidenced deep routes to safe public origins."""

    evidence = json.dumps(context, ensure_ascii=False)
    issues = []
    actions = []
    for action in candidate.actions:
        action = dict(action)
        if action.get("capability") != "open_url":
            actions.append(action)
            continue
        fixed_args = dict(action.get("fixed_args") or {})
        url = str(fixed_args.get("url") or "").strip()
        parsed = urlsplit(url)
        public_origin = bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
        if not public_origin and url not in evidence:
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
            ):
                fixed_args["url"] = f"{parsed.scheme}://{parsed.netloc}/"
                action["fixed_args"] = fixed_args
            else:
                issues.append(f"{action.get('name')}: navigation URL is not executable")
        actions.append(action)
    return candidate.model_copy(update={"actions": actions}), issues


class StrategyCandidate(StrictModel):
    """One falsifiable physical approach to an unchanged logical subgoal."""

    hypothesis: str = Field(min_length=1)
    invalidated_assumption: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    # Keep action drafts raw until they are merged into the complete WorkerSpec.
    # WorkerSpec owns provider-shape normalization and executable validation.
    actions: list[dict[str, Any]] = Field(min_length=1, max_length=12)
    expected_progress: str = Field(min_length=1)
    disconfirming_evidence: str = Field(min_length=1)
    evidence_basis: list[str] = Field(min_length=1)
    estimated_steps: int = Field(ge=1, le=20)
    acquisition_filters: dict[str, Any] | None = None

    def worker_spec(
        self,
        original: WorkerSpec,
        *,
        preserve_acquisition_filters: bool,
    ) -> WorkerSpec:
        """Materialize only the mutable physical portion of a WorkerSpec."""

        filters = (
            original.acquisition_filters
            if preserve_acquisition_filters
            else self.acquisition_filters
        )
        return WorkerSpec.model_validate({
            **original.model_dump(mode="python"),
            "strategy": self.strategy,
            "actions": self.actions,
            "acquisition_filters": filters,
        })


class StrategyProposal(StrictModel):
    # Candidate validation is intentionally isolated in choose(); one malformed
    # alternative must not erase executable siblings from the same response.
    candidates: list[dict[str, Any]] = Field(min_length=1, max_length=3)


class StrategySelection(StrictModel):
    decision: Literal["attempt", "stop"]
    chosen_index: int | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _decision_matches_index(self) -> "StrategySelection":
        if self.decision == "attempt" and self.chosen_index is None:
            raise ValueError("attempt requires chosen_index")
        if self.decision == "stop" and self.chosen_index is not None:
            raise ValueError("stop must not include chosen_index")
        if self.chosen_index is not None and self.chosen_index < 0:
            raise ValueError("chosen_index must be non-negative")
        return self


class StrategyPlanner:
    """Generate genuine alternatives, then select independently or stop."""

    def __init__(
        self,
        proposer: Any,
        *,
        proposer_model: str = "",
        selector: Any | None = None,
        selector_model: str = "",
        explicit_cache: bool = False,
    ) -> None:
        self.proposer = proposer
        self.proposer_model = proposer_model
        self.selector = selector or proposer
        self.selector_model = selector_model or proposer_model
        self.explicit_cache = explicit_cache

    def choose(
        self,
        *,
        context: dict[str, Any],
        original_spec: WorkerSpec,
        preserve_acquisition_filters: bool,
        validate: Callable[[WorkerSpec], list[str]],
        on_event: Callable[..., None],
    ) -> tuple[WorkerSpec | None, str]:
        proposal, response, messages, elapsed = self._invoke(
            system=_PROPOSE_SYSTEM,
            payload=context,
            schema=StrategyProposal,
            max_tokens=2_500,
            model=self.proposer,
            model_name=self.proposer_model,
        )
        valid: list[tuple[int, StrategyCandidate, WorkerSpec]] = []
        reviewed: list[dict[str, Any]] = []
        for proposal_index, raw_candidate in enumerate(proposal.candidates):
            candidate: StrategyCandidate | None = None
            spec: WorkerSpec | None = None
            issues: list[str] = []
            try:
                candidate = StrategyCandidate.model_validate(raw_candidate)
                candidate, issues = _ground_navigation_urls(candidate, context)
                spec = candidate.worker_spec(
                    original_spec,
                    preserve_acquisition_filters=preserve_acquisition_filters,
                )
                candidate = candidate.model_copy(update={
                    "actions": [
                        action.model_dump(mode="python") for action in spec.actions
                    ],
                })
                issues.extend(validate(spec))
            except Exception as exc:  # one malformed candidate must not erase others
                issues = [f"{type(exc).__name__}: {exc}"]
            remaining_steps = context.get("remaining_step_budget")
            if (
                candidate is not None
                and isinstance(remaining_steps, int)
                and candidate.estimated_steps > remaining_steps
            ):
                issues.append(
                    f"estimated_steps exceeds remaining budget ({remaining_steps})"
                )
            reviewed.append({
                "proposal_index": proposal_index,
                "candidate": (
                    candidate.model_dump(mode="json")
                    if candidate is not None
                    else raw_candidate
                ),
                "worker_spec": spec.model_dump(mode="json") if spec else {},
                "diagnostics": issues,
            })
            if candidate is not None and spec is not None and not issues:
                valid.append((proposal_index, candidate, spec))
        on_event(
            "strategy_candidates_proposed",
            candidates=reviewed,
            llm_elapsed_s=round(elapsed, 3),
            token_usage=response_usage(response),
            context_reports=diagnostic_prompt_reports(
                "tool_agent.strategy_propose",
                messages,
                response,
                parsed={"candidates": reviewed},
                schema="StrategyProposal",
            ),
        )
        if not valid:
            return None, "No proposed strategy satisfied the executable Worker contract."
        valid.sort(key=lambda item: item[1].estimated_steps)

        selection, response, messages, elapsed = self._invoke(
            system=_SELECT_SYSTEM,
            payload={
                "replan_reason": context.get("replan_reason"),
                "remaining_step_budget": context.get("remaining_step_budget"),
                "attempted_strategies": context.get("attempted_strategies"),
                "candidates": [
                    {
                        "index": index,
                        "proposal_index": proposal_index,
                        "candidate": candidate.model_dump(mode="json"),
                    }
                    for index, (proposal_index, candidate, _spec) in enumerate(valid)
                ],
            },
            schema=StrategySelection,
            max_tokens=500,
            model=self.selector,
            model_name=self.selector_model,
        )
        chosen_proposal_index = (
            valid[selection.chosen_index][0]
            if selection.chosen_index is not None
            and selection.chosen_index < len(valid)
            else None
        )
        on_event(
            "strategy_selected",
            decision=selection.decision,
            chosen_index=selection.chosen_index,
            chosen_proposal_index=chosen_proposal_index,
            reason=selection.reason,
            llm_elapsed_s=round(elapsed, 3),
            token_usage=response_usage(response),
            context_reports=diagnostic_prompt_reports(
                "tool_agent.strategy_select",
                messages,
                response,
                parsed=selection.model_dump(mode="json"),
                schema="StrategySelection",
            ),
        )
        if selection.decision == "stop":
            return None, selection.reason
        assert selection.chosen_index is not None
        if selection.chosen_index >= len(valid):
            return None, "Strategy Selector chose an unavailable candidate."
        return valid[selection.chosen_index][2], selection.reason

    def _invoke(
        self,
        *,
        system: str,
        payload: dict[str, Any],
        schema: type[BaseModel],
        max_tokens: int,
        model: Any,
        model_name: str,
    ) -> tuple[BaseModel, Any, list[Any], float]:
        messages = [
            cacheable_system_message(system, enabled=self.explicit_cache),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        generator = model.bind(
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            **chat_request_kwargs(model_name),
        )
        started_at = time.perf_counter()
        response = generator.invoke(messages)
        elapsed_s = time.perf_counter() - started_at
        try:
            value = schema.model_validate(parse_json_object(response.content))
        except Exception as exc:
            raise ValueError(f"{schema.__name__} returned invalid JSON: {exc}") from exc
        return value, response, messages, elapsed_s


__all__ = [
    "StrategyCandidate",
    "StrategyPlanner",
    "StrategyProposal",
    "StrategySelection",
]
