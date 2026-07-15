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


def test_program_runtime_owns_scheduling_and_supervisor_cannot_walk_dag() -> None:
    """Ownership: ProgramRuntime always-on; supervisor step without reseed fails."""
    import inspect

    from gui_agent.core.run import loop as loop_mod
    from gui_agent.core.run import program_runtime as prt
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    loop_src = inspect.getsource(loop_mod.run_agent_loop)
    assert "ensure_program" in loop_src
    assert "ProgramRuntime.start" in loop_src

    policy_src = inspect.getsource(MilestoneSupervisorPolicy.step)
    assert "_decompose" not in policy_src
    assert "reseed" in policy_src or "requires reseed" in policy_src

    advance_src = inspect.getsource(MilestoneSupervisorPolicy._advance)
    assert "_next_milestone" not in advance_src

    assert prt.ensure_program(None, "g").statements


def test_statement_outcome_is_terminal_only_and_not_a_second_state_machine() -> None:
    """Ownership: StatementOutcome has no running phase; mid-loop uses ExecutorDecision."""
    from gui_agent.core.run.statements import outcome as outcome_mod

    source = inspect.getsource(outcome_mod.StatementOutcome)
    assert "running" not in source or 'no "running"' in source or "no running" in source
    assert "def completed(" in source
    assert "def failed(" in source
    assert "def infeasible(" in source

    # Mid-turn decisions are a separate type — not StatementOutcome variants.
    decision_source = inspect.getsource(outcome_mod.ExecutorDecision)
    assert "act" in decision_source
    assert "observe" in decision_source
    assert "phase" not in decision_source

    # Interactive mapping never invents a running outcome for mid-loop steps.
    mid = type("S", (), {
        "goal_completed": False,
        "stop": False,
        "replan_directive": None,
        "summary": "go",
        "stop_reason": "",
        "completion_status": "in_progress",
        "should_act": True,
        "instruction": "tap",
        "is_loading": False,
        "preformed_action": None,
    })()
    assert outcome_mod.statement_outcome_from_supervisor_step(mid) is None
    decision = outcome_mod.executor_decision_from_supervisor_step(mid)
    assert decision is not None and decision.kind == "act"
