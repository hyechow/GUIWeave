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
