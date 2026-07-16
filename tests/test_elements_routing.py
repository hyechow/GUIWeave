"""Transition receives focused element knowledge without a selector control decision."""

from __future__ import annotations

from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement import llm_runtime as runtime_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
)


def _statement(name: str = "创建订单") -> StatementContract:
    return StatementContract(
        id="x",
        name=name,
        description="配置当前业务对象",
        success_condition="目标配置已完成",
        kind="action",
    )


def _captured_elements(monkeypatch, policy: StatementSupervisorPolicy, statement: StatementContract):
    captured: dict = {}

    def fake_transition(*args, **kwargs):
        captured.update(kwargs)
        return _StatementTransitionResult(
            kind="act",
            reason="continue",
            action=_TransitionAction(instruction="open the visible target"),
        )

    monkeypatch.setattr(runtime_module, "run_statement_transition", fake_transition)
    policy.begin_statement(statement, instance_id="i1")
    observation = Observation(png_bytes=b"x", source="test", title=statement.name)
    policy._invoke_statement_transition(
        statement,
        observation,
        [],
        memory_view=build_memory_view(
            instance_id="i1",
            contract=statement,
            history=[],
        ),
    )
    return captured.get("elements_knowledge")


def test_transition_uses_deterministically_matched_sections(monkeypatch):
    policy = StatementSupervisorPolicy()
    policy.set_app_knowledge(
        "nav",
        elements="FULL_ELEMENTS_BLOB",
        sections={"创建订单": "点快速建单", "连通性": "点检测"},
    )

    out = _captured_elements(monkeypatch, policy, _statement())

    assert out is not None
    assert "点快速建单" in out
    assert "点检测" not in out
    assert "FULL_ELEMENTS_BLOB" not in out


def test_transition_falls_back_to_full_blob_without_sections(monkeypatch):
    policy = StatementSupervisorPolicy()
    policy.set_app_knowledge("nav", elements="FULL_ELEMENTS_BLOB", sections=None)

    assert _captured_elements(monkeypatch, policy, _statement()) == "FULL_ELEMENTS_BLOB"


def test_transition_uses_none_when_no_element_knowledge(monkeypatch):
    policy = StatementSupervisorPolicy()
    policy.set_app_knowledge("nav", elements="", sections=None)

    assert _captured_elements(monkeypatch, policy, _statement()) is None
