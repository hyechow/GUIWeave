"""Bridge between the DSL orchestrator and the existing per-milestone executor (agent_loop).

The agent_loop IS the runner; this module only TRANSLATES between the DSL's Run/RunResult
and the supervisor's Milestone, and packages a finished milestone's loop state into a
RunResult. The agent_loop drives Interpreter.steps(), reseeds the supervisor per Run via
to_milestone()/task_type_for(), and on milestone-done calls package_result().

Branch note: on feat-android there is no `inspect`/`read_spec` (that's the parked
feat-read-spec). So a `read` Run maps to collection + read_once and reads come back as
unstructured content_notes text — structured {field: value} extraction is step #3.
"""

from __future__ import annotations

from typing import Literal

from gui_agent.core.schemas import Milestone

from .program import Run
from .runner import RunResult

# DSL RunKind -> feat-android (kind, completion_strategy).
_KIND_MAP: dict[str, tuple[str, str]] = {
    "navigation": ("navigation", "visible_once"),
    "filter": ("filter", "visible_once"),
    "action": ("action", "visible_once"),
    "read": ("collection", "read_once"),
}


def to_milestone(run: Run, index: int) -> Milestone:
    """Build a feat-android Milestone the supervisor can drive from a DSL Run spec.

    `returns` (fields to read) are folded into the description so the read instruction
    targets them; structured {field: value} extraction is a later step (#3)."""
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    desc = run.name
    if run.returns:
        desc = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    return Milestone(
        id=run.var or f"m{index}_{run.kind}",
        name=run.name,
        description=desc,
        success_condition=run.success_condition or f"完成「{run.name}」",
        kind=kind,  # type: ignore[arg-type]  # validated against MilestoneKind Literal
        completion_strategy=strategy,  # type: ignore[arg-type]
    )


def task_type_for(run: Run) -> Literal["action", "analysis"]:
    """A read Run -> 'analysis' so the supervisor's task_type-gated reader actually reads
    (feat-android gates reading on task_type; see policy._ctx / _default_read_instruction)."""
    return "analysis" if run.kind == "read" else "action"


def package_result(
    run: Run, *, completed: bool, summary: str, notes: list[str],
    reads: dict[str, str] | None = None,
) -> RunResult:
    """Package a finished milestone's loop state into the RunResult contract. `reads` is the
    structured {field: value} extracted from the result frame for a read milestone (see
    orchestrator.structured_read); other milestones pass none."""
    return RunResult(
        completed=completed,
        failed=not completed,
        reads=dict(reads) if reads else {},
        summary=summary,
        evidence=list(notes),
    )
