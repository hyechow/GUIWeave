"""Regression coverage for authoritative DOM controls in Statement Transition.

The old replanner-specific path is gone.  The invariant remains: a narrow visual input must
not hide its authoritative DOM value from the single LLM transition decision.
"""

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.supervisor.statement import model_io
from gui_agent.core.supervisor.statement import policy as statement_policy
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
)


def _statement() -> StatementContract:
    return StatementContract.model_validate(
        {
            "id": "m3",
            "name": "在 Product 列筛选框输入 'Olivia zip jacket'",
            "description": "d",
            "success_condition": "Product 框内容为 'Olivia zip jacket'",
            "kind": "action",
        }
    )


def test_transition_human_blocks_include_dom_form_controls(monkeypatch):
    captured: dict = {}

    def _fake_assemble(prompt, observation, *, system_blocks=None, human_blocks=None, **kw):
        captured["system_blocks"] = [b for b in (system_blocks or []) if b is not None]
        captured["human_blocks"] = [b for b in (human_blocks or []) if b is not None]
        return [{"role": "user", "content": "x"}]

    monkeypatch.setattr(model_io, "assemble_messages", _fake_assemble)
    monkeypatch.setattr(
        model_io,
        "invoke_structured",
        lambda *a, **k: _StatementTransitionResult(
            kind="act",
            reason="权威控件状态尚未满足",
            action=_TransitionAction(instruction="继续当前可见操作"),
        ),
    )

    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[
            {
                "label": "Product",
                "kind": "text_input",
                "value": "Olivia zip jacket",
                "focused": True,
            }
        ],
    )
    model_io.run_statement_transition(
        _statement(),
        observation,
        memory_view=build_memory_view(
            instance_id="test:transition",
            contract=_statement(),
            history=[],
        ),
        acceptance_knowledge="保存成功提示表示本次提交已被接受",
    )

    text = " ".join(getattr(block, "content", "") for block in captured["human_blocks"])
    assert "浏览器 DOM 表单控件" in text
    assert 'current="Olivia zip jacket"' in text
    assert "保存成功提示表示本次提交已被接受" in text


def test_transition_preserves_atomic_execution_contract(monkeypatch):
    policy = StatementSupervisorPolicy()
    statement = StatementContract(
        id="open-products",
        name="enter products list",
        description="",
        success_condition="products list is visible",
        kind="navigation",
    )
    policy.begin_statement(statement, instance_id="test:transition")
    monkeypatch.setattr(statement_policy, "is_loading_frame", lambda _observation: False)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: _StatementTransitionResult(
                kind="act",
                reason="展开菜单后激活目标入口",
                action=_TransitionAction(
                instruction="点击展开菜单中的 Products 链接",
                atomic_role="prepare",
                action_family="navigate",
                target_control="Products",
            ),
        ),
    )

    step = policy.step(
        Observation(png_bytes=b"fixture", source="browser"),
        goal="",
        history=[],
    )

    assert step.action_intent.instruction == "Open the visible 'Products' entry."
    assert step.action_intent.role == "prepare"
    assert step.action_intent.family == "navigate"
    assert step.action_intent.target_control == "Products"
