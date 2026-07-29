import pytest

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.core.run.action_signals import (
    build_action_signal,
    effective_action_role,
    semantic_action_key,
)
from gui_agent.core.schemas import (
    ActionIntent,
    BaseAction,
    BaseActionDecision,
    Observation,
    SupervisorStep,
    TargetBinding,
)


def _step(role="commit") -> SupervisorStep:
    return SupervisorStep(
        action_intent=ActionIntent(
            instruction="click Save",
            role=role,
            family="activate",
            target_control="Save",
        ),
        summary="save current form",
        statement_id="edit",
        execution_scope="i1:edit/statement",
    )


def test_commit_identity_does_not_depend_on_pointer_coordinates():
    first = BaseAction(action_type="tap", x=400, y=800, description="Save")
    second = BaseAction(action_type="tap", x=700, y=800, description="Save")
    assert semantic_action_key(_step(), first) == semantic_action_key(_step(), second)


def test_exact_transport_refs_remain_distinct_mechanical_actions():
    first = BrowserAction(
        action_type="scroll_to_ref", target_ref=41, description="show target"
    )
    second = BrowserAction(
        action_type="scroll_to_ref", target_ref=42, description="show target"
    )
    assert semantic_action_key(_step("iterate"), first) != semantic_action_key(
        _step("iterate"), second
    )


def test_concrete_scroll_is_recorded_as_iterate_not_commit():
    action = BaseAction(
        action_type="scroll",
        direction="down",
        description="show lower content",
    )
    assert effective_action_role(_step("commit"), action) == "iterate"


def test_commit_role_follows_adapter_declared_control_geometry():
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        form_controls=[{
            "kind": "button",
            "label": "Persist form",
            "form_action": "commit",
            "rect": {"x": 800, "y": 20, "w": 100, "h": 40},
        }],
    )

    assert effective_action_role(
        _step("prepare"),
        BaseAction(
            action_type="tap",
            x=790,
            y=10,
            description="Persist form",
            snap={"method": "dom", "snapped": [850, 40]},
        ),
        observation,
    ) == "commit"
    assert effective_action_role(
        _step("commit"),
        BaseAction(action_type="tap", x=500, y=500, description="Apply child changes"),
        observation,
    ) == "write"


_SAVE_CONTROL = {
    "kind": "button",
    "label": "Save",
    "form_action": "commit",
    "rect": {"x": 800, "y": 20, "w": 100, "h": 40},
}
_DRAG = BaseAction(
    action_type="drag",
    x=500,
    y=500,
    to_x=900,
    to_y=500,
    description="drag control",
)


@pytest.mark.parametrize(
    ("source", "role", "family", "target", "action", "controls", "expected"),
    [
        (
            "browser", "prepare", "activate", "Apply Filters",
            BaseAction(action_type="tap", x=850, y=40, description="apply"),
            [{**_SAVE_CONTROL, "query_action": "submit", "form_action": None}], "commit",
        ),
        (
            "android", "prepare", "activate", "Airplane mode switch",
            BaseAction(action_type="tap", x=500, y=500, description="toggle"), [], "commit",
        ),
        ("android", "prepare", "activate", "Brightness slider", _DRAG, [], "commit"),
        ("android", "write", "input", "Threshold slider", _DRAG, [_SAVE_CONTROL], "write"),
        ("iphone", "write", "input", "Minute picker", _DRAG, [], "write"),
        (
            "browser", "write", "activate", "Feature switch",
            BaseAction(action_type="tap", x=500, y=500, description="toggle"),
            [_SAVE_CONTROL], "write",
        ),
    ],
    ids=(
        "query-submit",
        "mobile-switch",
        "mobile-slider",
        "staged-mobile-slider",
        "picker",
        "browser-form-switch",
    ),
)
def test_effective_action_role_boundaries(
    source, role, family, target, action, controls, expected
) -> None:
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="act on control",
            role=role,
            family=family,
            target_control=target,
        ),
        summary="act",
    )
    observation = Observation(
        png_bytes=b"x",
        source=source,
        form_controls=controls or None,
    )

    assert effective_action_role(step, action, observation) == expected


def test_action_signal_records_dispatch_fact_without_business_effect_state():
    intent = ActionIntent(
        instruction="enter 31 in Size",
        role="write",
        family="input",
        target_control="Size",
        target_value="31",
    )
    step = SupervisorStep(
        action_intent=intent,
        summary="enter size",
        statement_id="edit",
        execution_scope="i1:edit/statement",
    )
    decision = BaseActionDecision(
        action=BaseAction(
            action_type="type", x=500, y=500, text="31", description="enter 31"
        )
    )
    binding = TargetBinding(
        status="bound", source="structural", unit_id="field:size"
    )
    signal = build_action_signal(
        step,
        decision,
        role="write",
        action_key=semantic_action_key(step, decision.action),
        surface_id="main",
        executed=True,
        suppressed_reason="",
        binding=binding,
    )

    assert signal is not None
    assert signal.execution == "dispatched"
    assert signal.mutation_receipt is not None
    assert signal.mutation_receipt.intended_value == "31"
    assert "effect" not in type(signal).model_fields
