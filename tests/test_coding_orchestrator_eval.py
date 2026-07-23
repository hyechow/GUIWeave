from __future__ import annotations

from gui_agent.core.coding_orchestrator.models import (
    CodingAttempt,
    CodingPlan,
    CodingReview,
    CodingRunResult,
)
from gui_agent.core.orchestrator import Program
from gui_agent.core.orchestrator.decomposer import _PlanDraft, _StepDraft, to_program
from gui_agent.core.orchestrator.program import ObservationBinding, OutputSpec, ValueRef
from scripts.coding_orchestrator_eval import (
    _coding_sample,
    coding_verdict,
    grade_dsl_program,
)


def test_reviewed_sample_uses_prompt_review_not_frozen_task_answer(
    monkeypatch,
) -> None:
    plan = CodingPlan(
        goal="perform the requested task",
        source="def run(ctx):\n    assert ctx, 'runtime exists'",
        attempts=[CodingAttempt(
            source="def run(ctx):\n    assert ctx, 'runtime exists'",
            run=CodingRunResult(ok=True),
        )],
        review=CodingReview(text='{"approve": true, "edits": []}', approved=True),
    )
    monkeypatch.setattr(
        "scripts.coding_orchestrator_eval.generate_reviewed_code",
        lambda *args, **kwargs: plan,
    )

    sample = _coding_sample(
        {"task_id": 778, "intent": "perform the requested task"},
        "",
        None,
    )

    assert sample["ok"]
    assert sample["requirements_satisfied"]
    assert plan.review is not None
    assert sample["evaluation_mode"] == "prompt_review"
    assert sample["failures"] == []


def test_coding_verdict_separates_functional_and_stability_gates() -> None:
    summary = {"executable_rate": 1.0}

    single = coding_verdict(summary, {778: 1}, samples_per_task=1)
    stable = coding_verdict(summary, {778: 3}, samples_per_task=5)

    assert single == {"coding_functionally_viable": True}
    assert stable == {
        "coding_functionally_viable": True,
        "coding_stability_gate": True,
    }


def test_task_549_dsl_grader_uses_same_business_state_requirements() -> None:
    draft = _PlanDraft(steps=[
        _StepDraft(
            op="interact",
            goal="add XXXL option to existing Size attribute",
            success="Size attribute option XXXL saved",
            required_values={"attribute": "size", "option": "XXXL"},
            persistence="explicit_commit",
        ),
        _StepDraft(
            op="interact",
            goal="configure Minerva product",
            success="configurable product configuration green XXXL saved",
            required_values={"product": "Minerva", "color": "green", "size": "XXXL"},
            persistence="explicit_commit",
        ),
    ])
    program = to_program(draft, "configure product")

    assert isinstance(program, Program)
    assert grade_dsl_program(549, program) == []
