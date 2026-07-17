"""Offline evals for browser applied-filter state.

These cases lock down that adapter filter facts validate an LLM completion proposal regardless
of the adapter-specific evidence channel (modern chips vs legacy grid URL/filter row).
"""

from __future__ import annotations

import json
from pathlib import Path

import gui_agent.core.supervisor.statement.policy as policy_mod
from gui_agent.core.schemas import StatementContract, Observation
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAssessment,
    _TransitionEvidence,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CASES = Path(__file__).with_name("cases.json")


def _png() -> bytes:
    fixture = PROJECT_ROOT / "evals/browser/checker/screenshots/products_qty3_filter_salable_distractor.png"
    return fixture.read_bytes() if fixture.exists() else b"\x89PNG\r\n\x1a\n"


def test_filter_state_cases(monkeypatch):
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert cases

    transition_calls: list[str] = []

    def _transition(*_args, **_kwargs):
        transition_calls.append("called")
        return _StatementTransitionResult(
            assessment=_TransitionAssessment(
                status="satisfied",
                summary="the exact declared filter is visible",
                established_facts=["adapter reports the filter as applied"],
            ),
            kind="complete",
            reason="the exact declared filter is visible",
            evidence=[_TransitionEvidence(
                source="current_observation",
                claim="the adapter reports the declared filter as applied",
            )],
        )

    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    for case in cases:
        transition_calls.clear()
        statement = StatementContract(
            id="m_filter",
            name=case["statement_name"],
            description=case["statement_name"],
            success_condition=case["success_condition"],
            kind="filter",
            target_values=case.get("target_values", {}),
        )
        policy = policy_mod.StatementSupervisorPolicy()
        policy.begin_statement(statement, instance_id=f"eval:{case['label']}")
        policy._invoke_statement_transition = _transition  # type: ignore[method-assign]
        obs = Observation(
            png_bytes=_png(),
            source="eval",
            applied_filters=case.get("applied_filters"),
            applied_filter_meta=case.get("applied_filter_meta"),
            tables=[{
                "caption": "Reviews",
                "row_count": case.get("grid_total_records"),
                "total_records": case.get("grid_total_records"),
            }],
        )

        step = policy.step(obs, goal=case["goal"], history=[])

        if case.get("expect_filter_gate_done"):
            assert transition_calls == ["called"], case["label"]
            assert step is not None and step.outcome is not None, case["label"]
            assert step.outcome.phase == "completed", case["label"]
        else:
            assert transition_calls == ["called", "called"], case["label"]
            assert step.outcome is not None and step.outcome.phase == "exhausted", case["label"]
