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
    _evaluate_hidden_source,
    coding_verdict,
    fixture_for_task,
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
        coding_eval_mode="whitebox",
    )

    assert sample["ok"]
    assert sample["requirements_satisfied"]
    assert plan.review is not None
    assert sample["evaluation_mode"] == "whitebox_regression"
    assert sample["review_fixture_visible"]
    assert sample["hidden_evaluation"] is None


def test_blind_sample_hides_fixture_until_final_evaluation(monkeypatch) -> None:
    captured = {}
    source = (
        "def run(ctx):\n"
        "    rows = ctx.query('sahara', fields=['id', 'name'], coverage='complete')\n"
        "    assert rows, 'matching products are required'\n"
        "    return len(rows)"
    )
    plan = CodingPlan(
        goal="perform the requested task",
        source=source,
        attempts=[CodingAttempt(
            source=source,
            run=CodingRunResult(ok=True),
        )],
        review=CodingReview(text='{"approve": true, "edits": []}', approved=True),
    )

    def generate(*args, **kwargs):
        captured["fixture"] = kwargs.get("fixture")
        return plan

    monkeypatch.setattr(
        "scripts.coding_orchestrator_eval.generate_reviewed_code",
        generate,
    )

    sample = _coding_sample(
        {"task_id": 778, "intent": "perform the requested task"},
        "",
        None,
        coding_eval_mode="blind",
    )

    assert captured["fixture"] is None
    assert sample["evaluation_mode"] == "blind_generalization"
    assert not sample["review_fixture_visible"]
    assert sample["hidden_evaluation"] is not None
    assert sample["failures"] == [], sample["hidden_evaluation"]


def test_task_193_hidden_fixture_checks_the_numeric_result() -> None:
    source = (
        "def run(ctx):\n"
        "    rows = ctx.query('orders', field='ID', filters={'Status': 'Complete'}, "
        "fields=['Status', 'Purchase Date', 'Grand Total (Purchased)'], "
        "coverage='complete')\n"
        "    assert all(row['Status'] == 'Complete' for row in rows), "
        "'status filter must hold'\n"
        "    assert len(rows) >= 2, 'at least two completed orders are required'\n"
        "    latest = sorted(rows, key=lambda row: row['Purchase Date'], reverse=True)[:2]\n"
        "    return round(sum(row['Grand Total (Purchased)'] for row in latest), 2)"
    )

    result, ok = _evaluate_hidden_source(
        source,
        fixture_for_task(193),
        expected_return=182.4,
    )

    assert ok
    assert result["return_value"] == 182.4
    assert result["return_matches"]
    filter_events = [
        event for event in result["trace"]
        if event["op"] == "query"
        and event["kwargs"]["filters"] == {"Status": "Complete"}
    ]
    assert filter_events


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
