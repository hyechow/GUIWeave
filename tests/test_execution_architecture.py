from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from gui_agent.core.run.execution_signals import CompletionEvaluation
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy


POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "gui_agent/core/supervisor/milestone/policy.py"
)


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


def test_done_state_writes_are_limited_to_completion_or_explicit_delegation():
    owners: list[tuple[str, int]] = []
    for function in _function_nodes(_policy_tree()):
        for node in ast.walk(function):
            if not isinstance(node, ast.Assign):
                continue
            if not (
                isinstance(node.value, ast.Constant)
                and node.value.value == "done"
            ):
                continue
            if any(
                isinstance(target, ast.Attribute)
                and target.attr == "status"
                for target in node.targets
            ):
                owners.append((function.name, node.lineno))

    assert owners
    assert {name for name, _line in owners} == {"_advance", "_try_filter_fallback"}

    fallback = next(
        node for node in _function_nodes(_policy_tree())
        if node.name == "_try_filter_fallback"
    )
    fallback_source = ast.unparse(fallback)
    assert "self._completion_evaluator.decide" in fallback_source
    assert "decision.status != 'delegated'" in fallback_source


def test_advance_requires_keyword_only_complete_decision():
    decision_param = inspect.signature(
        MilestoneSupervisorPolicy._advance
    ).parameters["decision"]
    assert decision_param.kind is inspect.Parameter.KEYWORD_ONLY
    assert decision_param.default is inspect.Parameter.empty

    policy = MilestoneSupervisorPolicy()
    milestone = Milestone.model_validate({
        "id": "m",
        "name": "perform one statement",
        "description": "",
        "success_condition": "postcondition is confirmed",
        "kind": "action",
    })
    observation = Observation(png_bytes=b"png", source="test")

    with pytest.raises(ValueError, match="cannot advance without satisfied"):
        policy._advance(
            milestone,
            observation,
            [],
            decision=CompletionEvaluation("pending", "insufficient evidence"),
        )
