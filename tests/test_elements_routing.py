"""Transition receives focused element knowledge without a selector control decision."""

from __future__ import annotations

from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import CollectionIntent, Observation, StatementContract
from gui_agent.core.supervisor.statement import llm_runtime as runtime_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
)


def _statement(name: str = "创建订单") -> StatementContract:
    return StatementContract(
        id="x",
        goal=name,
        success="目标配置已完成",
    )


def _captured_elements(monkeypatch, policy: StatementSupervisorPolicy, statement: StatementContract):
    captured: dict = {}

    def fake_transition(*args, **kwargs):
        captured.update(kwargs)
        return _StatementTransitionResult(
            assessment=_TransitionAssessment(
                status="in_progress",
                summary="target is not open",
                open_gaps=["open the target"],
            ),
            kind="act",
            reason="continue",
            action=_TransitionAction(
                instruction="在当前可见区域激活目标控件",
                action_family="activate",
                target_control="visible target",
                expected_result="target content becomes visible",
            ),
        )

    monkeypatch.setattr(runtime_module, "run_statement_transition", fake_transition)
    policy.begin_statement(statement, instance_id="i1")
    observation = Observation(png_bytes=b"x", source="test", title=statement.goal)
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


def test_transition_does_not_load_a_section_from_one_generic_overlap(monkeypatch):
    policy = StatementSupervisorPolicy()
    policy.set_app_knowledge(
        "nav",
        elements="FULL_ELEMENTS_BLOB",
        sections={
            "Products": (
                "---\n"
                "selector_when: query filter products quantity\n"
                "---\n"
                "products body"
            ),
            "Orders": (
                "---\n"
                "selector_when: orders collection status\n"
                "---\n"
                "orders body"
            ),
            "Grid controls": (
                "---\n"
                "selector_when: filter grid view controls\n"
                "---\n"
                "grid controls body"
            ),
        },
    )
    statement = StatementContract(
        id="x",
        goal="Narrow the Products collection by Quantity",
        success="The Quantity filter is active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Products",
        ),
    )

    out = _captured_elements(monkeypatch, policy, statement)

    assert out is not None
    assert "products body" in out
    assert "grid controls body" in out
    assert "orders body" not in out
