from __future__ import annotations

import inspect

from gui_agent.core.run.action_exec import ActionExecutionState
from gui_agent.core.run.action_ledger import ActionLedger
from gui_agent.core.run.execution_signals import CompletionEvaluator
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


def test_milestone_policy_is_the_only_component_with_control_flow_authority() -> None:
    policy_source = inspect.getsource(MilestoneSupervisorPolicy)
    executor_source = inspect.getsource(ActionExecutionState)
    ledger_source = inspect.getsource(ActionLedger)
    evaluator_source = inspect.getsource(CompletionEvaluator)

    assert "def _advance(" in policy_source
    assert "def _handle_stuck(" in policy_source
    assert "authorize_action_dispatch" not in policy_source

    assert "validate_action_family" not in executor_source
    assert "authorize_action_dispatch" not in executor_source
    assert "def authorize(" not in ledger_source
    assert "validate_proposal" not in evaluator_source


def test_planner_metadata_is_not_a_dispatch_gate() -> None:
    source = inspect.getsource(ActionExecutionState.run)

    assert "action_family" not in source
    assert "suppressed_reason" not in source


def test_policy_does_not_execute_structural_target_units_directly() -> None:
    source = inspect.getsource(MilestoneSupervisorPolicy._run_single_turn)

    assert "target_unit_state" not in source
    assert "target_unit_execution_plan" not in source
    assert "ambiguous_target_unit" not in source


def test_completion_evaluator_only_evaluates_evidence() -> None:
    public = {
        name
        for name, value in CompletionEvaluator.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert public == {"decide"}
