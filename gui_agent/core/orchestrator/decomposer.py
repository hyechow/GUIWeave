"""Program Decomposer: user goal -> DSL Program (the orchestrator's #2).

Replaces the milestone-DAG decompose with a PROGRAM decompose: a goal becomes a small
sequence of milestone-level run() statements plus control flow (if / finish). The LLM
produces a flat, LLM-friendly draft (an explicit `op` per step, a `reasoning` CoT field
up front — rigid schemas suppress reasoning, see structured_read), which we convert to
the clean Program AST deterministically and validate (an if must branch on a real read,
a read must request fields, a finish template must resolve) with one feedback-retry —
the cheap deterministic backstop pattern, not a string-match band-aid.
"""

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
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from gui_agent.core.router import IntentResolution, intent_block
from .program import Program
from ._decomposer.draft import _FunctionDraft, _PlanDraft, _StepDraft, _to_stmts, to_program
from ._decomposer.context import (
    _corrective_directive_block,
    _page_and_table_blocks,
    _prior_experience_block,
    _remaining_plan_block,
    _table_schema_prompt,
)
from ._decomposer.sql import (
    _normalize_approximate_entity_sql,
    _normalize_data_query_display_identifiers,
)
from .intent_contracts import IntentContractIssue, validate_intent_contracts
from .validator import (  # validator lives in its own module; decompose imports it back
    ValidationIssue,
    validate_program,
)

_SYSTEM = load_prompt_text("task.orchestrator.decomposer")
# Re-decompose reuses the FULL decomposer prompt (DSL grammar + rules 1-10 + examples) and appends a
# "mid-execution revision" framing — the output schema/validation is identical; only the framing
# (re-plan the REMAINING steps from the CURRENT page, absorbing prior experience) differs.
_REDECOMPOSE_SYSTEM = _SYSTEM + "\n\n" + load_prompt_text("task.orchestrator.redecomposer")


# Validator repairs can cascade: fixing one structural issue can expose a second issue that was
# previously masked by the invalid draft. Keep a small bounded extra attempt so the LLM sees the
# newly surfaced validator feedback once, without turning compile into an open-ended loop.
_MAX_RETRIES = 3

__all__ = [
    "OrchestratorCompileError",
    "decompose",
    "redecompose",
    "to_program",
    "validate_program",
    "_FunctionDraft",
    "_PlanDraft",
    "_StepDraft",
    "_to_stmts",
    "_normalize_approximate_entity_sql",
    "_normalize_data_query_display_identifiers",
    "_table_schema_prompt",
]


class OrchestratorCompileError(RuntimeError):
    """Raised when LLM draft repair retries are exhausted with validator issues still present."""

    def __init__(self, issues: list[ValidationIssue], program: Program) -> None:
        self.issues = list(issues)
        self.program = program
        joined = "; ".join(str(issue) for issue in self.issues[:3])
        suffix = "" if len(self.issues) <= 3 else f"; ... (+{len(self.issues) - 3})"
        super().__init__(f"orchestrator compile validation failed: {joined}{suffix}")


def _contract_issue_to_validation_issue(issue: IntentContractIssue) -> ValidationIssue:
    return ValidationIssue(
        issue.code,
        issue.message,
        severity=issue.severity,
        evidence=issue.evidence,
    )


def _merge_feedback_issues(
    prior: list[ValidationIssue],
    current: list[ValidationIssue],
) -> list[ValidationIssue]:
    """Carry validator feedback forward across repair attempts.

    Repair prompts are constraints, not one-shot hints. If attempt N fixes issue A but attempt N+1
    drops it while fixing issue B, a "last attempt only" feedback block creates validator
    whack-a-mole. Keep the small de-duplicated history for the current compile call so later
    retries preserve earlier repairs without adding any production normalizer.
    """
    out: list[ValidationIssue] = []
    seen: set[tuple[str, str]] = set()
    for issue in [*prior, *current]:
        key = (issue.code, str(issue))
        if key in seen:
            continue
        out.append(issue)
        seen.add(key)
    return out


def _invoke_plan(
    *,
    system_prompt: str,
    png_bytes: bytes | None,
    context_blocks: list["ContextBlock | None"],
    goal: str,
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None,
    context_reports: list[dict] | None,
    label: str,
    attempt_observer: "Callable[[int, list[ValidationIssue]], None] | None" = None,
    resolution: "IntentResolution | None" = None,
) -> Program:
    """Shared LLM call + deterministic validate/feedback-retry. Both decompose() and redecompose()
    assemble their own context blocks, then hand off here for the identical draft→AST→validate loop.
    `resolution` (when the caller has one) additionally arms intent-contract checks
    (router entity coverage / set selector membership / entity-scope predicates) so all generation
    entrances share the same repair feedback."""
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    llm = ChatOpenAI(
        model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
        extra_body={"enable_thinking": False},
    )
    issues: list[ValidationIssue] = []
    feedback_issues: list[ValidationIssue] = []
    previous_draft = ""
    program = Program(goal=goal, statements=[])
    for attempt in range(_MAX_RETRIES + 1):
        messages = assemble_messages(
            system_prompt,
            png_bytes,
            human_blocks=[
                *context_blocks,
                feedback_block(feedback_issues, previous_output=previous_draft),
            ],
            image_resize="none",
            prepare_vision_prompt_png=prepare_vision_prompt_png,
            label=label,
            context_reports=context_reports,
            decision_text="",
        )
        draft = invoke_structured(llm, messages, _PlanDraft, trace_sink=context_reports, trace_label=label)
        previous_draft = draft.model_dump_json(exclude_defaults=True, exclude_none=True)
        program = to_program(draft, goal)
        program = _normalize_data_query_display_identifiers(program)
        all_issues = list(validate_program(program, resolution=resolution))
        if resolution is not None:
            all_issues.extend(
                _contract_issue_to_validation_issue(issue)
                for issue in validate_intent_contracts(program, resolution)
            )
        issues = [issue for issue in all_issues if getattr(issue, "severity", "error") == "error"]
        if attempt_observer is not None:
            # Offline instrumentation only (default None ⇒ production path unchanged): record the
            # codes that fired on each draft so the retry-efficacy harness can measure, per code,
            # whether feeding it back actually clears it on the next attempt. See
            # scripts/validator_retry_efficacy.py.
            attempt_observer(attempt, list(all_issues))
        if not issues:
            break
        feedback_issues = _merge_feedback_issues(feedback_issues, issues)
        if attempt < _MAX_RETRIES:
            print(f"  [Orchestrator] 程序分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{_MAX_RETRIES})...")
            for i in issues:
                print(f"  [Orchestrator]   {i}")
    if issues:
        print(f"  [Orchestrator] 程序分解校验仍有 {len(issues)} 项问题，停止出厂。")
        for i in issues:
            print(f"  [Orchestrator]   {i}")
        raise OrchestratorCompileError(issues, program)
    return program


def decompose(
    goal: str,
    *,
    png_bytes: bytes | None = None,
    knowledge: str = "",
    file_section: str = "",
    system_prompt: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
    table_summaries: list[dict] | None = None,
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
    corrective_directive: str = "",
    resolution: "IntentResolution | None" = None,
    attempt_observer: "Callable[[int, list[ValidationIssue]], None] | None" = None,
) -> Program:
    """Decompose a user goal into a DSL Program via LLM + deterministic validate/retry.

    `png_bytes` (current screen) gives the planner page context; `knowledge` injects app
    navigation knowledge; `file_section` is the resolved content of any `@<path>` refs in the
    goal (config field values the spoken goal only points at — see resolve_file_refs);
    `system_prompt` overrides the default DSL prompt (platform tuning); `resolution` is the
    router's upfront entity classification (fuzzy-allowed? which key?) — rendered as a FACTS-ONLY
    context block right after the goal (see intent_block); decompose owns translating it into the
    retrieval ladder (rule 4b), not the fuzzy/exact decision itself.
    `prepare_vision_prompt_png` is the platform bundle's vision prompt image hook:
    iPhone downscales Retina frames, browser/android keep native observations.
    """
    context_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        intent_block(resolution),
        _corrective_directive_block(corrective_directive),
        file_reference_block(file_section),
        knowledge_block("app_navigation", knowledge),
        *_page_and_table_blocks(current_url, current_site, current_title, table_summaries),
    ]
    program = _invoke_plan(
        system_prompt=system_prompt or _SYSTEM,
        png_bytes=png_bytes,
        context_blocks=context_blocks,
        goal=goal,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
        context_reports=context_reports,
        label="orchestrator.decompose",
        attempt_observer=attempt_observer,
        resolution=resolution,
    )
    from .passes import finalize_gates
    return finalize_gates(_normalize_approximate_entity_sql(program, resolution))


def redecompose(
    goal: str,
    *,
    remaining_plan: str = "",
    prior_experience: str = "",
    corrective_directive: str = "",
    png_bytes: bytes | None = None,
    knowledge: str = "",
    file_section: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
    table_summaries: list[dict] | None = None,
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
    resolution: "IntentResolution | None" = None,
) -> Program:
    """Re-decompose the REMAINING (unexecuted) plan mid-run — NOT a fresh full-goal decompose.

    Unlike `decompose` (goal → full plan from the start screen), this is invoked after a Feasibility
    kick-back: some milestones already ran (their outcomes are `prior_experience`), one hit a
    correction (`corrective_directive`), and the rest (`remaining_plan`) must be re-planned from the
    CURRENT page (current_url/title/png/table_summaries reflect where the run actually is now, not
    its start). Reuses the full DSL prompt + schema + validation; only the framing differs (see
    redecomposer.md). The returned Program covers only the remaining work.
    """
    context_blocks: list[ContextBlock | None] = [
        task_goal_block(goal),
        intent_block(resolution),
        _corrective_directive_block(corrective_directive),
        _prior_experience_block(prior_experience),
        _remaining_plan_block(remaining_plan),
        file_reference_block(file_section),
        knowledge_block("app_navigation", knowledge),
        *_page_and_table_blocks(current_url, current_site, current_title, table_summaries),
    ]
    program = _invoke_plan(
        system_prompt=_REDECOMPOSE_SYSTEM,
        png_bytes=png_bytes,
        context_blocks=context_blocks,
        goal=goal,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
        context_reports=context_reports,
        label="orchestrator.redecompose",
        resolution=resolution,
    )
    from .passes import finalize_gates
    return finalize_gates(_normalize_approximate_entity_sql(program, resolution))
