"""Compile a user goal into the six-node semantic Program IR."""

from __future__ import annotations

from collections.abc import Callable

from langchain_openai import ChatOpenAI

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import (
    feedback_block,
    file_reference_block,
    knowledge_block,
    task_goal_block,
)
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.router import IntentResolution
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from ._decomposer.draft import _PlanDraft, _StepDraft, _to_stmts, to_program
from ._validator.issue import ValidationIssue
from .program import Program
from .validator import validate_program


_SYSTEM = load_prompt_text("task.orchestrator.decomposer")
_REDECOMPOSE_SYSTEM = _SYSTEM + "\n\n" + load_prompt_text("task.orchestrator.redecomposer")
_MAX_REPAIRS = 1


class OrchestratorCompileError(RuntimeError):
    def __init__(self, issues: list[ValidationIssue], program: Program) -> None:
        self.issues = list(issues)
        self.program = program
        super().__init__(
            "orchestrator compile validation failed: "
            + "; ".join(str(issue) for issue in issues[:4])
        )


def _intent_facts(resolution: IntentResolution | None) -> ContextBlock | None:
    if resolution is None or not resolution.entities:
        return None
    lines = []
    for entity in resolution.entities:
        members = list(entity.value_members or [])
        lines.append(
            f"- mention={entity.mention!r}; role={entity.role}; type={entity.type}; "
            f"members={members}; range={(entity.selector or '')!r}; "
            f"search_hint={(entity.search_key or '')!r}"
        )
    return ContextBlock(
        id="runtime.intent_facts",
        budget="required",
        source_type="runtime_state",
        source="intent_resolver",
        ttl="task",
        priority=21,
        content=(
            "## Intent facts\n"
            "这些是值、范围和检索提示，不是检索步骤模板。Program 必须保留目标值与范围，"
            "具体字段、控件和检索方法由运行时 Interact 决定。\n"
            + "\n".join(lines)
        ),
    )


def _location_block(site: str, title: str, url: str) -> ContextBlock | None:
    values = [part for part in (site, title, url) if part]
    if not values:
        return None
    return ContextBlock(
        id="runtime.main_location",
        budget="normal",
        source_type="runtime_state",
        source="main_surface",
        ttl="turn",
        priority=30,
        content="## Current main location\n" + " | ".join(values),
    )


def _value_contract_issues(
    program: Program,
    resolution: IntentResolution | None,
) -> list[ValidationIssue]:
    if resolution is None:
        return []
    payload = program.model_dump_json(exclude={"id"}).casefold()
    issues: list[ValidationIssue] = []
    for entity in resolution.entities:
        if entity.role in {"target_value", "qualifier_value"}:
            values = list(entity.value_members or []) or [entity.mention]
        elif entity.role == "collection_scope":
            values = [entity.selector or entity.mention]
        else:
            continue
        missing = [value for value in values if str(value).casefold() not in payload]
        if missing:
            issues.append(
                ValidationIssue(
                    "ROUTER_VALUE_NOT_PRESERVED",
                    f"Router 声明的目标值/范围 {missing} 未进入 Program 的语义目标或参数",
                    evidence=tuple(missing),
                )
            )
    return issues


def _compile(
    *,
    system_prompt: str,
    goal: str,
    context_blocks: list[ContextBlock | None],
    context_reports: list[dict] | None,
    resolution: IntentResolution | None,
    label: str,
    attempt_observer: Callable[[int, list[ValidationIssue]], None] | None = None,
) -> Program:
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    from llm.provider_config import dashscope_extra_body

    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        extra_body=dashscope_extra_body(cfg.model),
    )
    issues: list[ValidationIssue] = []
    previous = ""
    program = Program(goal=goal)
    for attempt in range(_MAX_REPAIRS + 1):
        messages = assemble_messages(
            system_prompt,
            None,
            human_blocks=[*context_blocks, feedback_block(issues, previous_output=previous)],
            image_resize="none",
            label=label,
            context_reports=context_reports,
            decision_text="",
        )
        draft = invoke_structured(
            llm,
            messages,
            _PlanDraft,
            trace_sink=context_reports,
            trace_label=label,
        )
        previous = draft.model_dump_json(exclude_defaults=True, exclude_none=True)
        program = to_program(draft, goal)
        issues = [*validate_program(program), *_value_contract_issues(program, resolution)]
        if attempt_observer:
            attempt_observer(attempt, list(issues))
        if not issues:
            return program
        if attempt < _MAX_REPAIRS:
            print(f"  [Orchestrator] 语义 Program 有 {len(issues)} 项结构问题，修复一次...")
            for issue in issues:
                print(f"  [Orchestrator]   {issue}")
    raise OrchestratorCompileError(issues, program)


def decompose(
    goal: str,
    *,
    knowledge: str = "",
    file_section: str = "",
    system_prompt: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
    context_reports: list[dict] | None = None,
    corrective_directive: str = "",
    resolution: IntentResolution | None = None,
    attempt_observer: Callable[[int, list[ValidationIssue]], None] | None = None,
) -> Program:
    corrective = (
        ContextBlock(
            id="runtime.corrective_directive",
            budget="required",
            source_type="runtime_state",
            source="program_runtime",
            ttl="turn",
            priority=10,
            content="## Runtime correction\n" + corrective_directive,
        )
        if corrective_directive
        else None
    )
    return _compile(
        system_prompt=system_prompt or _SYSTEM,
        goal=goal,
        context_blocks=[
            task_goal_block(goal),
            _intent_facts(resolution),
            corrective,
            file_reference_block(file_section),
            knowledge_block("app_navigation", knowledge),
            _location_block(current_site, current_title, current_url),
        ],
        context_reports=context_reports,
        resolution=resolution,
        label="orchestrator.decompose",
        attempt_observer=attempt_observer,
    )


def redecompose(
    goal: str,
    *,
    remaining_plan: str = "",
    prior_experience: str = "",
    corrective_directive: str = "",
    **kwargs,
) -> Program:
    extra = "\n\n".join(
        part
        for part in (
            f"已完成事实：\n{prior_experience}" if prior_experience else "",
            f"剩余语义工作：\n{remaining_plan}" if remaining_plan else "",
            f"运行时纠正：\n{corrective_directive}" if corrective_directive else "",
        )
        if part
    )
    return decompose(
        goal,
        system_prompt=_REDECOMPOSE_SYSTEM,
        corrective_directive=extra,
        **kwargs,
    )


__all__ = [
    "OrchestratorCompileError",
    "_PlanDraft",
    "_StepDraft",
    "_to_stmts",
    "decompose",
    "redecompose",
    "to_program",
]
