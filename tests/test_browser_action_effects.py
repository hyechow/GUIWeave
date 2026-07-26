from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.policies import BrowserActionPolicy
from gui_agent.core.schemas import ActionIntent, Observation, SupervisorStep, TargetBinding


def test_browser_effect_resolution_uses_binding_and_primitives() -> None:
    policy = BrowserActionPolicy()
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="activate",
            family="activate",
            target_control="a label that is absent from observation",
        ),
        summary="activate",
    )
    tap = BrowserActionDecision(
        action=BrowserAction(action_type="tap", x=10, y=20, description="tap")
    )
    binding = TargetBinding(
        status="bound",
        source="structural",
        unit_id="ref:18",
        effect_kind="query_control",
    )
    assert policy.resolve_action_effect(
        step,
        Observation(png_bytes=b"png", source="browser"),
        tap,
        binding,
    ) == "query_control"

    columns = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[{
            "kind": "button",
            "label": "ID",
            "value": "Columns",
            "effect_kind": "presentation",
            "rect": {"x": 822, "y": 280, "w": 129, "h": 48},
        }],
    )
    columns_step = step.model_copy(update={
        "action_intent": step.action_intent.model_copy(
            update={"target_control": "Columns"}
        )
    })
    columns_tap = BrowserActionDecision(
        action=BrowserAction(action_type="tap", x=830, y=276, description="tap")
    )
    columns_binding = policy.bind(columns_step, columns, columns_tap)
    assert columns_binding is not None
    assert (columns_binding.status, columns_binding.effect_kind) == (
        "bound",
        "presentation",
    )
    assert policy.resolve_action_effect(
        columns_step, columns, columns_tap, columns_binding
    ) == "presentation"

    navigate = BrowserActionDecision(action=BrowserAction(
        action_type="navigate",
        url="https://example.test",
        description="navigate",
    ))
    assert policy.resolve_action_effect(
        SupervisorStep(action_intent=ActionIntent(
            instruction="navigate",
            family="navigate",
        ), summary="navigate"),
        Observation(png_bytes=b"png", source="browser"),
        navigate,
    ) == "navigation"
