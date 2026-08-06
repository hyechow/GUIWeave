"""Lock the action-feedback chain: repeated off_target on one control must turn
into a use-the-exact-ref corrective, and a meaningless icon label must fall back
to the structural resource id so the supervisor can bind it.

Reproduces the Mattermost channel-list "+" loop: the icon's key is a private
glyph, the LLM blind-estimates the same point turn after turn, every estimate is
rejected as off_target, and without a corrective it never converges.
"""

from __future__ import annotations

from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import (
    ActionIntent,
    ActionSignal,
    Observation,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
    TargetBinding,
)
from gui_agent.core.supervisor.statement.observation_view import (
    build_observation_view,
    _semantic_label,
)
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


PLUS_REF = "android:0.0.0.0.0.0.0.0.0.0.0.0.1.0.0.0.4.0"


def _contract() -> StatementContract:
    return StatementContract(
        id="c1",
        goal="create a channel",
        success="a channel was created",
    )


def _plus_tree() -> list[dict]:
    return [
        {
            "role": "button", "key": "\U000f0415",
            "resource": "channel_list_header.plus.button",
            "ref": PLUS_REF, "in_viewport": True,
            "point": {"x": 917.6, "y": 132.3},
        },
        {
            "role": "button", "key": "测试服务器图标",
            "resource": "channel_list.servers.server_icon",
            "ref": "android:0.0.0.0.0.0.0.0.0.0.0.0.1.0.0.0.1.0",
            "in_viewport": True, "point": {"x": 87.5, "y": 131.7},
        },
    ]


def test_icon_label_falls_back_to_resource_id() -> None:
    node = _plus_tree()[0]
    # The private-use glyph is meaningless → use the trailing resource segment.
    assert _semantic_label(node) == "plus"
    # A readable key always wins over the resource fallback.
    assert _semantic_label(_plus_tree()[1]) == "测试服务器图标"


def test_plus_affordance_exposes_semantic_label_and_ref() -> None:
    obs = Observation(png_bytes=b"", source="android", semantic_tree=_plus_tree())
    view = build_observation_view(_contract(), obs, [])
    plus = next((a for a in view.affordances if a.get("ref") == PLUS_REF), None)
    assert plus is not None
    assert plus["label"] == "plus"
    assert "activate" in plus["supported_operations"]


def _off_target_turn(index: int, control: str) -> PolicyTurn:
    intent = ActionIntent(
        instruction=f"tap {control}",
        role="write",
        family="activate",
        target_control=control,
    )
    return PolicyTurn(
        index=index,
        observation_source="android",
        statement_instance_id="i1:c1",
        supervisor=SupervisorStep(statement_id="c1", summary=f"t{index}", action_intent=intent),
        executed=False,
        action_signal=ActionSignal(
            role="write",
            execution="not_attempted",
            target="off_target",
            target_control=control,
            binding=TargetBinding(status="contradicted", source="structural", reason="off point"),
        ),
    )


def _memory_with_off_targets(controls: list[str]):
    history = [
        _off_target_turn(i + 1, c) for i, c in enumerate(controls)
    ]
    return build_memory_view(
        instance_id="i1:c1",
        contract=_contract(),
        history=history,
        observation=Observation(png_bytes=b"", source="android", semantic_tree=_plus_tree()),
    )


def test_repeated_off_target_forces_ref_binding() -> None:
    obs = Observation(png_bytes=b"", source="android", semantic_tree=_plus_tree())
    view = build_observation_view(_contract(), obs, [])
    memory = _memory_with_off_targets(["plus 按钮", "plus 按钮", "plus 按钮"])
    feedback = StatementSupervisorPolicy._grounding_ref_feedback(memory, view)
    assert "target_ref" in feedback
    assert "off_target" in feedback
    assert PLUS_REF in feedback  # points the supervisor at the exact ref


def test_single_off_target_does_not_trigger_corrective() -> None:
    obs = Observation(png_bytes=b"", source="android", semantic_tree=_plus_tree())
    view = build_observation_view(_contract(), obs, [])
    memory = _memory_with_off_targets(["plus 按钮"])
    assert StatementSupervisorPolicy._grounding_ref_feedback(memory, view) == ""


def test_distinct_off_targets_do_not_trigger_corrective() -> None:
    obs = Observation(png_bytes=b"", source="android", semantic_tree=_plus_tree())
    view = build_observation_view(_contract(), obs, [])
    # Different controls each miss once — no single repeated failure to correct.
    memory = _memory_with_off_targets(["plus 按钮", "搜索框", "设置图标"])
    assert StatementSupervisorPolicy._grounding_ref_feedback(memory, view) == ""
