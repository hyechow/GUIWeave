"""LLM-facing checker, selector, planner, and replanner bridge for milestone execution.

The mixin owns prompt assembly and model invocation only. Execution state transitions,
completion arbitration, and recovery remain in ``policy.py``.
"""

from __future__ import annotations

from typing import Optional

from langchain_openai import ChatOpenAI

from gui_agent.context.runtime import (
    constraints_block,
    extra_instruction_block,
    form_controls_block,
    history_block,
    knowledge_block,
    loop_frame_summary_block,
    milestone_block,
    replan_state_block,
    tried_instructions_block,
)
from gui_agent.core.run.execution_signals import ConstraintLedger
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn
from gui_agent.core.self_learning.progressive import _norm as _norm_page
from llm.structured import invoke_structured

from .execution_scope import execution_scope_for, page_known, route_identity_evidence
from .evidence import resolved_filter_intent
from .model_io import (
    _make_llm,
    assemble_messages,
    run_checker,
    run_loop_check,
    run_planner,
    run_selector,
)
from .runtime import _Timer
from .schemas import _LoopFrameResult, _PlanResult, _ReplanResult, _SingleCheckResult


class MilestoneLLMRuntimeMixin:
    """Prompt/model bridge mixed into ``MilestoneSupervisorPolicy``."""

    _constraint_ledger: ConstraintLedger

    def _single_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
        execution_scope: str = "",
        response_history: Optional[list[PolicyTurn]] = None,
    ) -> _SingleCheckResult:
        app_name = self._app_name
        if not app_name:
            for turn in reversed(history):
                if turn.supervisor and turn.supervisor.app_name:
                    app_name = turn.supervisor.app_name
                    break
        if milestone.completion_strategy == "react_until_collected" and self._collection_progress:
            extra = f"{extra}\n{self._collection_progress}".strip()
        identity_evidence = route_identity_evidence(milestone, observation)
        if identity_evidence:
            extra = f"{extra}\n{identity_evidence}".strip()
        runtime_filter = resolved_filter_intent(
            milestone,
            observation,
            history,
            scope=execution_scope or execution_scope_for(milestone, observation),
        )
        return run_checker(
            milestone,
            observation,
            history,
            app_name=app_name,
            task_type=self.task_type,
            constraints=self._constraints_for_scope(execution_scope),
            extra=extra,
            prompts=self._prompts,
            check_knowledge=self._check_knowledge,
            context_reports=self._context_reports,
            state_trace_text=self._monitor.render(scope=execution_scope),
            last_action_response=self._last_action_response_text(
                response_history or history
            ),
            initial_filters=self._initial_filters,
            runtime_filter=runtime_filter,
        )

    def _loop_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> _LoopFrameResult:
        return run_loop_check(
            milestone,
            observation,
            history,
            constraints=self._constraints_for_scope(),
            prompts=self._prompts,
            context_reports=self._context_reports,
        )

    def _select_sections(self, milestone: Milestone, check: _SingleCheckResult) -> list[str]:
        """Select focused knowledge sections, with deterministic fallback and page cache."""
        if self._pk is None:
            return []
        page_id = check.page_identity or ""
        is_known_page = page_known(page_id)
        key = (milestone.id, _norm_page(page_id))
        if is_known_page and key in self._selector_cache:
            stems = self._selector_cache[key]
            self._record_selector_report(
                milestone=milestone,
                page_identity=page_id,
                page_known=is_known_page,
                cache="hit",
                sections=stems,
                cached=True,
            )
            return stems
        signals = [page_id, milestone.name, milestone.success_condition]
        try:
            with _Timer(self._timings, self._timings_order, "selector", self._token_usage):
                selection = run_selector(
                    self._goal,
                    milestone,
                    page_id,
                    self._pk.selector_manifest(),
                    prompts=self._prompts,
                    context_reports=self._context_reports,
                )
            stems = self._pk.by_ids(selection.section_ids)
            selected_stems = list(stems)
            stems = self._pk.augment_with_signals(stems, signals)
            fallback_triggered = stems != selected_stems
            fallback_reason = ""
            if fallback_triggered:
                fallback_reason = (
                    "empty_selector" if not selected_stems else "deterministic_augmentation"
                )
            if stems or selection.section_ids:
                names = "、".join(stems) if stems else "（ID 未命中）"
                print(
                    f"  [Selector] {names}"
                    + (f" — {selection.reason}" if selection.reason else "")
                )
            if is_known_page:
                self._selector_cache[key] = stems
            self._record_selector_report(
                milestone=milestone,
                page_identity=page_id,
                page_known=is_known_page,
                cache="miss",
                section_ids=list(selection.section_ids or []),
                sections=stems,
                fallback_triggered=fallback_triggered,
                fallback_reason=fallback_reason,
                cached=is_known_page,
                reason=selection.reason,
            )
            return stems
        except Exception as exc:  # noqa: BLE001 - selector must never block the planner
            print(f"  [Selector] 调用失败，回退确定性模糊匹配：{exc}")
            stems = self._pk.match_signals(signals)
            self._record_selector_report(
                milestone=milestone,
                page_identity=page_id,
                page_known=is_known_page,
                cache="miss",
                sections=stems,
                fallback_triggered=bool(stems),
                fallback_reason="selector_error",
                cached=False,
                error=str(exc),
            )
            return stems

    def _record_selector_report(
        self,
        *,
        milestone: Milestone,
        page_identity: str,
        page_known: bool,
        cache: str,
        section_ids: list[str] | None = None,
        sections: list[str] | None = None,
        fallback_triggered: bool = False,
        fallback_reason: str = "",
        cached: bool = False,
        reason: str = "",
        error: str = "",
    ) -> None:
        self._context_reports.append({
            "kind": "selector",
            "label": "knowledge.selector",
            "milestone_id": milestone.id,
            "page_identity": page_identity,
            "page_known": page_known,
            "cache": cache,
            "section_ids": list(section_ids or []),
            "sections": list(sections or []),
            "fallback_triggered": fallback_triggered,
            "fallback_reason": fallback_reason,
            "cached": cached,
            "reason": reason,
            "error": error,
        })

    def _elements_for(self, milestone: Milestone, check: _SingleCheckResult) -> Optional[str]:
        if self._pk:
            self._last_sections_loaded = self._select_sections(milestone, check)
            return self._pk.bodies(self._last_sections_loaded)
        return self._elements_knowledge

    def _invoke_planner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _PlanResult:
        elements = self._elements_for(milestone, check)
        if milestone.completion_strategy == "react_until_collected" and self._collection_progress:
            extra = f"{extra}\n{self._collection_progress}".strip()
        runtime_filter = resolved_filter_intent(
            milestone,
            observation,
            history,
            scope=execution_scope_for(milestone, observation),
        )
        return run_planner(
            milestone,
            check,
            observation,
            history,
            constraints=self._constraints_for_scope(),
            extra=extra,
            app_knowledge=self._app_knowledge,
            elements_knowledge=elements,
            prompts=self._prompts,
            context_reports=self._context_reports,
            initial_filters=self._initial_filters,
            runtime_filter=runtime_filter,
        )

    def _invoke_loop_scroll(
        self,
        milestone: Milestone,
        frame: _LoopFrameResult,
        observation: Observation,
    ) -> _PlanResult:
        prompt = self._prompts.loop_scroll
        plan_schema = self._prompts.plan_result_schema or _PlanResult
        return invoke_structured(
            self._llm(),
            assemble_messages(
                prompt,
                observation,
                system_blocks=[
                    milestone_block(milestone),
                    constraints_block(self._constraints_for_scope()),
                    loop_frame_summary_block(frame.summary),
                ],
                image_resize=self._prompts.image_resize,
                label="loop_scroll",
                context_reports=self._context_reports,
            ),
            plan_schema,
            trace_sink=self._context_reports,
            trace_label="loop_scroll",
        )

    def _invoke_replanner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _ReplanResult:
        tried = sorted({
            turn.supervisor.instruction
            for turn in history
            if turn.supervisor
            and turn.supervisor.instruction
            and turn.supervisor.milestone_id == milestone.id
        })
        messages = assemble_messages(
            self._prompts.replan,
            observation,
            system_blocks=[
                milestone_block(milestone),
                replan_state_block(
                    check,
                    retry_count=milestone.retry_count,
                    failure_hints=milestone.failure_hints,
                ),
                constraints_block(self._constraints_for_scope()),
                history_block(history, current_milestone_id=milestone.id),
                tried_instructions_block(tried),
                extra_instruction_block(extra, source="replanner_guard"),
            ],
            human_blocks=[
                knowledge_block("app_navigation", self._app_knowledge),
                knowledge_block("page_elements", self._elements_for(milestone, check)),
                form_controls_block(
                    getattr(observation, "form_controls", None),
                    getattr(observation, "form_controls_meta", None),
                ),
            ],
            image_resize=self._prompts.image_resize,
            label="replanner",
            context_reports=self._context_reports,
        )
        result = invoke_structured(
            self._llm(),
            messages,
            _ReplanResult,
            trace_sink=self._context_reports,
            trace_label="replanner",
        )
        if self._is_sequence(result.instruction):
            print("  [Replan] 多步序列，重试...")
            result = self._invoke_replanner(
                milestone,
                check,
                observation,
                history,
                extra="你刚才输出了多个步骤，请只返回一个原子操作。",
            )
        return result

    def _llm(self) -> ChatOpenAI:
        return _make_llm()


__all__ = ["MilestoneLLMRuntimeMixin"]
