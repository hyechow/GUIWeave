"""Offline evals for browser applied-filter state.

These cases exercise deterministic filter gates, not LLM checker behavior. They lock down the
contract that `Observation.applied_filters` means "current applied filter set" regardless of the
adapter-specific evidence channel (modern chips vs legacy grid URL/filter row).
"""

from __future__ import annotations

import json
from pathlib import Path

import gui_agent.core.supervisor.statement.policy as policy_mod
from gui_agent.core.schemas import StatementContract, Observation


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CASES = Path(__file__).with_name("cases.json")


class _CheckerReached(Exception):
    pass


def _png() -> bytes:
    fixture = PROJECT_ROOT / "evals/browser/checker/screenshots/products_qty3_filter_salable_distractor.png"
    return fixture.read_bytes() if fixture.exists() else b"\x89PNG\r\n\x1a\n"


def test_filter_state_cases(monkeypatch):
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert cases

    checker_calls: list[str] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append("called")
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    for case in cases:
        checker_calls.clear()
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
        policy._single_check = _spy_run_checker  # type: ignore[method-assign]
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

        try:
            step = policy.step(obs, goal=case["goal"], history=[])
        except _CheckerReached:
            step = None

        if case.get("expect_filter_gate_done"):
            assert checker_calls == [], case["label"]
            assert step is not None and step.outcome is not None, case["label"]
            assert step.outcome.phase == "completed", case["label"]
        else:
            assert checker_calls == ["called"], case["label"]
