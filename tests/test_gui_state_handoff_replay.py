from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_agent.core.orchestrator import CodingProgram, CodingProgramRuntime
from gui_agent.core.filter_contract import AppliedFilterState
from gui_agent.core.run.contracts import Acquire, Interact
from gui_agent.core.run.interactive import contract_for_interact
from gui_agent.core.schemas import (
    CollectionIntent,
    Observation,
    StatementOutcome,
)
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "replay"
    / "fixtures"
    / "browser"
    / "100005_gui_handoff"
)


def _observation() -> Observation:
    payload = json.loads((FIXTURE / "observation_turn_6.json").read_text())
    return Observation.model_validate({
        **payload,
        "png_bytes": b"replay",
        "applied_filter_state": AppliedFilterState(
            predicates={},
            coverage="complete",
            source="replay",
        ),
    })


def _complete_without_model(
    monkeypatch,
    invocation,
    observation: Observation,
    index: int,
):
    contract = contract_for_interact(invocation, index)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(
        contract,
        instance_id=f"i{index + 1}:{contract.id}",
    )
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail(
            "structured state should complete this phase mechanically"
        ),
    )
    step = policy._run_single_turn(contract, observation, [])
    assert step.outcome is not None and step.outcome.is_completed
    return step.outcome


def test_100005_hands_verified_gui_state_to_strict_query_lookup(
    monkeypatch,
    request,
) -> None:
    monkeypatch.setattr(
        policy_module,
        "is_loading_frame",
        lambda _observation: False,
    )
    source = """
def run(ctx, state):
    state = ctx.reach(
        state,
        "Open the Orders list under Sales",
        success={
            "entity": "Orders",
            "fields": ["Status", "Purchase Date"],
        },
    )
    scope = ctx.query(state, entity="Orders")
    return ctx.acquire(scope, fields=["Status", "Purchase Date"])
"""
    runtime = CodingProgramRuntime.start(
        CodingProgram(
            goal="Count completed orders by month",
            source=source,
        )
    )
    request.addfinalizer(runtime.close)
    observation = _observation()

    assert isinstance(runtime.current.statement, Interact)
    assert runtime.current.statement.interaction_intent is None
    assert runtime.current.statement.expected_state == {
        "entity": "Orders",
        "fields": ["Status", "Purchase Date"],
    }
    assert observation.tables[0]["total_records"] == 38
    assert observation.tables[0]["traversal"]["page_index"] == 2

    # c1 owns only the visible GUI state. Collection identity belongs to the
    # query's following locate phase.
    runtime.send_outcome(
        StatementOutcome.completed(
            "Orders GUI state is visible",
            verification="accepted_unverified",
        )
    )

    # Control moves to query phase 1 and the same table resolves structurally.
    assert isinstance(runtime.current.statement, Interact)
    assert isinstance(
        runtime.current.statement.interaction_intent,
        CollectionIntent,
    )
    assert runtime.current.statement.interaction_intent.phase == "locate"
    assert runtime.current.args["ui_state_token"].endswith(":state")
    assert runtime.current.inputs["ui_state"]["observed_state"] == {}
    locate_outcome = _complete_without_model(
        monkeypatch,
        runtime.current,
        observation,
        1,
    )
    assert locate_outcome.outputs["scope"]["entity"] == "Orders"
    runtime.send_outcome(locate_outcome)

    # A query without predicates has nothing to reconcile. It moves directly
    # from the located collection state to materialization.
    assert isinstance(runtime.current.statement, Acquire)
