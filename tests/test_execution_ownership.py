from __future__ import annotations

import inspect

from gui_agent.core.run.action_exec import ActionExecutionState
from gui_agent.core.run import action_signals, turns
from gui_agent.core.run.execution_signals import ExecutionCoordinator
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone import evidence, observation_state
from gui_agent.core.supervisor.milestone.schemas import _PlanResult


def test_milestone_policy_is_the_only_component_with_control_flow_authority() -> None:
    policy_source = inspect.getsource(MilestoneSupervisorPolicy)
    executor_source = inspect.getsource(ActionExecutionState)
    signal_source = inspect.getsource(action_signals)
    evaluator_source = inspect.getsource(ExecutionCoordinator)

    assert "def _advance(" in policy_source
    assert "def _handle_stuck(" in policy_source
    assert "def _recover(" not in policy_source
    assert "authorize_action_dispatch" not in policy_source

    assert "validate_action_family" not in executor_source
    assert "authorize_action_dispatch" not in executor_source
    assert "def authorize(" not in signal_source
    assert "validate_proposal" not in evaluator_source


def test_only_write_target_binding_can_fail_safe_before_dispatch() -> None:
    source = inspect.getsource(ActionExecutionState.run)

    assert "action_family" not in source
    assert "effective_action_role(sv_step, action)" in source
    assert "bind_action_target" in source
    assert "target binding failed before dispatch" in source


def test_concrete_action_semantics_are_fixed_at_dispatch_boundary() -> None:
    executor_source = inspect.getsource(ActionExecutionState.run)
    recorder_source = inspect.getsource(turns.make_interactive_turn)

    assert "effective_action_role" in executor_source
    assert "semantic_action_key" in executor_source
    assert "effective_action_role" not in recorder_source
    assert "semantic_action_key" not in recorder_source


def test_action_signal_updates_have_one_runtime_writer() -> None:
    signal_source = inspect.getsource(action_signals)
    evidence_source = inspect.getsource(evidence)
    executor_source = inspect.getsource(ActionExecutionState)

    assert 'signal.response = "' in signal_source
    assert 'signal.target = "' in signal_source
    assert 'signal.response = "' not in evidence_source
    assert 'signal.target = "' not in evidence_source
    assert 'signal.response = "' not in executor_source
    assert 'signal.target = "' not in executor_source


def test_evidence_projects_receipts_instead_of_reading_live_progress_monitor() -> None:
    source = inspect.getsource(evidence)

    assert "ProgressMonitor" not in source
    assert "signal.response" in source


def test_policy_does_not_execute_structural_target_units_directly() -> None:
    source = inspect.getsource(MilestoneSupervisorPolicy._run_single_turn)

    assert "target_unit_state" not in source
    assert "target_unit_execution_plan" not in source
    assert "ambiguous_target_unit" not in source


def test_mutation_subject_has_one_runtime_owner() -> None:
    policy_source = inspect.getsource(MilestoneSupervisorPolicy)

    assert not hasattr(observation_state, "target_unit_state")
    assert not hasattr(observation_state, "required_group_field_gaps")
    assert "resolve_mutation" in policy_source
    assert "target_group_id" not in _PlanResult.model_fields


def test_execution_coordinator_has_one_public_decision_api() -> None:
    public = {
        name
        for name, value in ExecutionCoordinator.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert public == {"decide"}


def test_support_services_cannot_transition_milestones() -> None:
    for module in (evidence,):
        source = inspect.getsource(module)
        assert "._advance(" not in source
        assert "._handle_stuck(" not in source
        assert "milestone.status =" not in source
        assert "SupervisorStep(" not in source
