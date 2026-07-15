from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.run.statement_runtime import StatementRuntimeState
from gui_agent.core.schemas import StatementContract, Observation
from gui_agent.core.supervisor.statement.execution_scope import (
    resource_identity_from_url,
)
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "gui_agent/core/supervisor/statement/policy.py"
)
STATEMENT_DIR = POLICY_PATH.parent


def _policy_tree() -> ast.Module:
    return ast.parse(POLICY_PATH.read_text(encoding="utf-8"))


def _function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_every_advance_call_supplies_satisfied_completion_evidence():
    calls = [
        node
        for node in ast.walk(_policy_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_advance"
    ]

    assert calls
    for call in calls:
        assert any(keyword.arg == "decision" for keyword in call.keywords), (
            f"_advance call at line {call.lineno} bypasses completion evaluation"
        )


def test_statement_runtime_has_no_parallel_terminal_status():
    policy_source = POLICY_PATH.read_text(encoding="utf-8")

    assert "status" not in StatementRuntimeState.__dataclass_fields__
    assert "_set_status" not in policy_source
    assert "StatementOutcome.completed" in policy_source
    assert "StatementOutcome.failed" in policy_source


def test_advance_requires_keyword_only_complete_decision():
    decision_param = inspect.signature(
        StatementSupervisorPolicy._advance
    ).parameters["decision"]
    assert decision_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert decision_param.default is inspect.Parameter.empty

    policy = StatementSupervisorPolicy()
    statement = StatementContract.model_validate({
        "id": "m",
        "name": "perform one statement",
        "description": "",
        "success_condition": "postcondition is confirmed",
        "kind": "action",
    })
    observation = Observation(png_bytes=b"png", source="test")

    with pytest.raises(ValueError, match="cannot advance without satisfied"):
        policy._advance(
            statement,
            observation,
            [],
            decision=CompletionEvaluation("pending", "insufficient evidence"),
        )


def test_policy_does_not_reimplement_execution_services():
    policy_methods = {node.name for node in _function_nodes(_policy_tree())}

    assert "resolved_plan_action_family" not in policy_methods
    assert "is_terminal_dispatch_turn" not in policy_methods
    assert "resource_identity_from_url" not in policy_methods
    assert "target_value_claims" not in policy_methods
    assert "checker_claim" not in policy_methods


def test_execution_kernel_contains_no_site_or_benchmark_vocabulary():
    kernel_files = [
        STATEMENT_DIR / name
        for name in (
            "policy.py",
            "evidence.py",
            "execution_scope.py",
            "observation_state.py",
            "acquisition.py",
            "model_io.py",
        )
    ]
    forbidden = ("webarena", "magento", "shopping_admin")

    for path in kernel_files:
        source = path.read_text(encoding="utf-8").casefold()
        for token in forbidden:
            assert token not in source, f"{path.name} contains site vocabulary {token!r}"

        assert "gui_agent.adapters." not in source, (
            f"{path.name} imports a platform adapter from core execution"
        )


def test_prompt_text_postprocessors_are_not_execution_services():
    model_io_tree = ast.parse(
        (STATEMENT_DIR / "model_io.py").read_text(encoding="utf-8")
    )
    model_io_functions = {node.name for node in _function_nodes(model_io_tree)}

    assert not model_io_functions & {
        "_guard_native_select_plan",
        "_guard_exact_dropdown_target",
        "_guard_named_field_substitution_plan",
        "_guard_stale_text_filter_plan",
        "_repeated_candidate_click",
        "_reopens_selected_dropdown",
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://host/workspace/entity/edit/widget_id/42/", "workspace/entity/edit/widget_id/42"),
        ("https://host/app/detail/id/7/", "app/detail/id/7"),
        ("https://host/app/list/page/3/", ""),
        ("https://host/app/index/filter/name/value/", ""),
    ],
)
def test_execution_scope_uses_generic_resource_routes(url: str, expected: str):
    assert resource_identity_from_url(url) == expected
