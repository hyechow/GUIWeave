import ast
import inspect
from pathlib import Path

from gui_agent.core.orchestrator.runner import Interpreter
from gui_agent.core.run.action_exec import ActionExecutor
from gui_agent.core.run.statements.acquire import AcquireMemoryView
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.statement_memory import StatementMemoryView
from gui_agent.core.schemas import StatementOutcome
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def test_program_runtime_owns_program_scheduling_and_recovery():
    assert hasattr(ProgramRuntime, "send_outcome")
    assert hasattr(ProgramRuntime, "replace_program")
    assert hasattr(ProgramRuntime, "begin_kickback")
    policy = inspect.getsource(StatementSupervisorPolicy)
    for retired in (
        "self._statements",
        "self._order",
        "self._current_id",
        "_next_statement",
        "_terminal_step",
        "_decompose",
    ):
        assert retired not in policy


def test_statement_transition_does_not_own_a_business_phase_machine():
    fields = set(StatementMemoryView.__dataclass_fields__)
    assert "phase" not in fields
    assert "subphase" not in fields
    source = inspect.getsource(StatementSupervisorPolicy)
    assert "prepare→write" not in source
    assert "resolve_transition" not in source
    assert "_invoke_planner" not in source
    assert "_single_check" not in source
    assert "_invoke_statement_transition" in source


def test_acquire_memory_is_replay_projection_and_transition_has_no_collection_context():
    assert "phase" not in AcquireMemoryView.__dataclass_fields__
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "gui_agent/core/supervisor/statement/policy.py",
        "gui_agent/core/supervisor/statement/model_io.py",
        "gui_agent/core/supervisor/statement/llm_runtime.py",
    ):
        assert "collection_view" not in (root / relative).read_text(encoding="utf-8")


def test_interact_has_no_parallel_business_data_reader():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "gui_agent/core/run/content.py").exists()
    assert not (root / "gui_agent/core/llm/reader.py").exists()
    for relative in (
        "gui_agent/core/run/loop.py",
        "gui_agent/core/supervisor/statement/policy.py",
        "gui_agent/core/supervisor/statement/schemas.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "read_instruction" not in source
        assert "content_notes" not in source


def test_statement_outcome_is_terminal_only():
    assert "running" not in StatementOutcome.model_fields["phase"].annotation.__args__
    assert hasattr(StatementOutcome, "completed")
    assert hasattr(StatementOutcome, "infeasible")


def test_action_executor_has_one_dispatch_boundary():
    source = inspect.getsource(ActionExecutor.run)
    assert source.count("executor.execute(") == 1
    assert "bind_action_target" in source


def test_core_transition_prompt_contains_no_site_or_benchmark_vocabulary():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "gui_agent/core/supervisor/statement/policy.py",
        root / "gui_agent/prompts/tasks/statement/shared/transition.md",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8").casefold()
        for token in ("webarena", "magento", "shopping_admin", "mobile-world"):
            assert token not in source


def test_interpreter_has_no_runtime_subcompiler():
    source = inspect.getsource(Interpreter)
    assert "subdecompose" not in source
    assert "body_goal" not in source
    assert "expand" not in source


def test_adapter_recompile_callbacks_preserve_runtime_bindings():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "gui_agent/adapters/browser/webarena.py",
        "gui_agent/adapters/android/mobileworld.py",
    ):
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        callbacks = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_redecompose"
        ]
        assert callbacks
        assert all(
            "available_bindings" in {arg.arg for arg in node.args.kwonlyargs}
            for node in callbacks
        )
        assert all(
            any(
                isinstance(child, ast.Call)
                and any(keyword.arg == "available_bindings" for keyword in child.keywords)
                for child in ast.walk(node)
            )
            for node in callbacks
        )
