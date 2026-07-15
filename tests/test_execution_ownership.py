from __future__ import annotations

import inspect
import re
from pathlib import Path

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
    assert "ProgramRuntime.start" in loop_src
    assert "ensure_program" not in loop_src
    # Single-writer cursor: no parallel loop locals for interpreter cursor/kickback.
    assert "_cur_run" not in loop_src
    assert "_kickback_replans" not in loop_src
    assert "rt.current =" not in loop_src
    assert "rt.send_outcome" in loop_src or "send_outcome" in loop_src
    assert "rt.replace_program" in loop_src or "replace_program" in loop_src
    assert "statement_outcome_from_supervisor_step" not in loop_src

    policy_src = inspect.getsource(MilestoneSupervisorPolicy.step)
    assert "_decompose" not in policy_src
    assert "begin_statement" in policy_src or "requires begin_statement" in policy_src

    policy_src = inspect.getsource(MilestoneSupervisorPolicy)
    for retired in ("self._milestones", "self._order", "self._current_id", "_next_milestone", "_terminal_step"):
        assert retired not in policy_src
    assert "def begin_statement" in policy_src
    assert "def end_statement" in policy_src
    assert "def runtime_state_snapshot" not in policy_src

    assert not hasattr(prt, "ensure_program")
    assert not hasattr(prt, "compile_single_statement_program")
    assert hasattr(prt.ProgramRuntime, "send_outcome")
    assert hasattr(prt.ProgramRuntime, "replace_program")
    assert hasattr(prt.ProgramRuntime, "next_instance_id")
    assert not hasattr(prt.ProgramRuntime, "accept_dispatch_cursor")
    assert not hasattr(prt.ProgramRuntime, "send")

    from gui_agent.core.run.statements import drain_immediate_statements

    dispatch_parameters = inspect.signature(drain_immediate_statements).parameters
    assert "program_runtime" in dispatch_parameters
    assert "interpreter_steps" not in dispatch_parameters
    assert "current_statement" not in dispatch_parameters


def test_statement_outcome_is_terminal_only_and_not_a_second_state_machine() -> None:
    """Ownership: StatementOutcome has no running phase or turn-control variants."""
    import pytest

    from gui_agent.core.run.statements import outcome as outcome_mod
    from gui_agent.core.schemas import SupervisorStep

    with pytest.raises(ValueError):
        outcome_mod.StatementOutcome(phase="running", summary="mid")  # type: ignore[arg-type]

    assert hasattr(outcome_mod.StatementOutcome, "completed")
    assert hasattr(outcome_mod.StatementOutcome, "failed")
    assert hasattr(outcome_mod.StatementOutcome, "infeasible")

    assert not hasattr(outcome_mod, "ExecutorDecision")
    assert not hasattr(outcome_mod, "statement_outcome_from_supervisor_step")
    assert {
        "stop",
        "stop_reason",
        "goal_completed",
        "completion_status",
        "replan_directive",
    }.isdisjoint(SupervisorStep.model_fields)

    mid = SupervisorStep(
        summary="go",
        should_act=True,
        instruction="tap",
    )
    assert mid.outcome is None


def test_dsl_only_entrypoints_have_no_mode_switches() -> None:
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "gui_agent/core/run/cli.py",
        "gui_agent/adapters/android/mobileworld.py",
        "gui_agent/adapters/browser/webarena.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "--orchestrator" not in source
        assert "--no-orchestrator" not in source
        assert re.search(r"args\.orchestrator\b", source) is None
        assert re.search(r"args\.no_orchestrator\b", source) is None
