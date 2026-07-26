from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.policies import BrowserActionPolicy
from gui_agent.core.schemas import ActionIntent, Observation, SupervisorStep


def test_browser_effect_resolution_uses_structure_and_primitives() -> None:
    policy = BrowserActionPolicy()
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        semantic_tree=[{
            "ref": 18,
            "role": "button",
            "key": "Refund",
            "effect_kind": "query_control",
        }],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="activate",
            family="activate",
            target_control="Refund",
            target_ref="18",
        ),
        summary="activate",
    )
    tap = BrowserActionDecision(
        action=BrowserAction(action_type="tap", x=10, y=20, description="tap")
    )
    assert policy.resolve_action_effect(step, observation, tap) == "query_control"

    columns = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[{
            "kind": "button",
            "label": "ID",
            "value": "Columns",
            "effect_kind": "presentation",
        }],
    )
    assert policy.resolve_action_effect(
        step.model_copy(update={"action_intent": step.action_intent.model_copy(
            update={"target_control": "Columns", "target_ref": ""}
        )}),
        columns,
        tap,
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
