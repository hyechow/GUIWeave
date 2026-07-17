from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.target_binding import (
    BrowserTargetBinder,
)
from gui_agent.adapters.browser.policies import BrowserActionPolicy
from gui_agent.core.run.action_exec import ActionExecutor
from gui_agent.core.run.target_binding import bind_action_target
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Observation,
    SupervisorStep,
)


def _step(**updates) -> SupervisorStep:
    step = SupervisorStep(action_intent=ActionIntent(instruction='set Amount to 42', role='write', family='input', target_control='Amount', target_value='42'), summary='write the declared field', statement_id='m1', execution_scope='record:1', statement_kind='action')
    intent_updates = {
        {
            "atomic_role": "role",
            "action_family": "family",
        }.get(key, key): updates.pop(key)
        for key in list(updates)
        if key in {
            "atomic_role",
            "action_family",
            "target_control",
            "target_value",
            "direction",
            "drag_column",
            "drag_steps",
            "instruction",
        }
    }
    if intent_updates:
        updates["action_intent"] = step.action_intent.model_copy(
            update=intent_updates
        )
    return step.model_copy(update=updates)


def _decision(x: float | None = 400, y: float | None = 600) -> BaseActionDecision:
    return BaseActionDecision(action=BaseAction(
        action_type="type",
        x=x,
        y=y,
        text="42",
        description="set Amount to 42",
    ))


def test_visual_binding_requires_a_declared_target_and_concrete_point() -> None:
    observation = Observation(png_bytes=b"frame", source="visual")
    assert bind_action_target(
        binder=None,
        step=_step(),
        observation=observation,
        action_decision=_decision(),
    ).status == "bound"
    assert bind_action_target(
        binder=None,
        step=_step(),
        observation=observation,
        action_decision=_decision(None, None),
    ).status == "unresolved"
    assert bind_action_target(
        binder=None,
        step=_step(target_control=""),
        observation=observation,
        action_decision=_decision(),
    ).status == "unresolved"


def test_structural_binding_uses_the_point_owner_in_a_repeated_collection() -> None:
    controls = [
        {
            "label": "Amount",
            "kind": "text_input",
            "group_id": "record:1",
            "rect": {"x": 400, "y": 600},
        },
        {
            "label": "Amount",
            "kind": "text_input",
            "group_id": "record:2",
            "rect": {"x": 400, "y": 700},
        },
    ]
    observation = Observation(
        png_bytes=b"frame", source="browser", form_controls=controls
    )
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=400,
        y=600,
        text="42",
        description="set Amount to 42",
    ))

    bound = BrowserTargetBinder().bind(_step(), observation, decision)
    assert bound is not None
    assert (bound.status, bound.source, bound.unit_id) == (
        "bound",
        "structural",
        "record:1",
    )


def test_form_control_ref_does_not_collide_with_semantic_ref_namespace() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        semantic_tree=[],
        form_controls=[{
            "kind": "text_input",
            "label": "Attribute Code",
            "name": "attribute_code",
            "group_id": "attribute-grid",
            "rect": {"x": 167, "y": 408},
        }],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="在属性网格筛选区的 Attribute Code 输入框填写 size",
            role="write",
            family="input",
            target_control="Attribute Code",
            target_value="size",
            target_ref="attribute_code",
        ),
        summary="fill filter",
        statement_id="s2",
    )
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="type",
        x=167,
        y=408,
        text="size",
        description="填写 Attribute Code",
    ))

    binding = BrowserTargetBinder().bind(step, observation, decision)

    assert binding is not None
    assert (binding.status, binding.unit_id) == ("bound", "attribute-grid")


def test_opaque_dom_id_cannot_contradict_a_visual_choice_target() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[{
            "kind": "checkbox_input",
            "label": "generated-check-17",
            "id": "generated-check-17",
            "group_field": "generated-check-17",
            "group_id": "row:0",
            "rect": {"x": 154, "y": 527},
        }],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="在当前表格中点击 Color 行第一列的复选框",
            family="activate",
            target_control="Color row checkbox",
        ),
        summary="select color",
        statement_id="s1",
    )
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=154,
        y=527,
        description="点击 Color 行复选框",
    ))

    binding = BrowserTargetBinder().bind(step, observation, decision)

    assert binding is not None
    assert binding.status == "unresolved"
    assert "no semantic label" in binding.reason


def test_native_select_binds_via_target_option_despite_a_bad_label() -> None:
    # Replay of log 20260715_215953 Turn 19: the Type <select> (name=type_id) was labeled
    # 'notice-EV92REG' by the adapter, so name/label matching could not confirm it as the
    # declared 'Type' control and the action was suppressed (3x → run died). The option list
    # is authoritative, so a unique owning select that carries the declared target option
    # binds deterministically — without mutation authorization or a correct label.
    controls = [
        {
            "kind": "native_select",
            "label": "notice-EV92REG",
            "name": "type_id",
            "id": "EV92REG",
            "options": [
                "Simple Product", "Virtual Product", "Bundle Product",
                "Downloadable Product", "Configurable Product", "Grouped Product",
            ],
            "is_filter": True,
            "rect": {"x": 856, "y": 570, "w": 246, "h": 32},
        },
    ]
    observation = Observation(png_bytes=b"frame", source="browser", form_controls=controls)
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="select_option",
        x=854,
        y=569,
        text="Configurable Product",
        description="选择下拉选项 Configurable Product",
    ))
    step = _step(
        statement_kind="filter",
        action_family="select",
        target_control="Type",
        target_value="Configurable Product",
    )

    binding = BrowserTargetBinder().bind(step, observation, decision)

    assert binding is not None
    assert (binding.status, binding.source) == ("bound", "structural")


def test_identity_gap_is_unresolved_not_contradicted() -> None:
    # A declared target whose point-owner cannot be name-confirmed is an identity GAP
    # (unresolved), not a contradiction — there is no positive evidence of a *different*
    # declared control. (Was wrongly 'contradicted', which suppressed correct actions whose
    # label the adapter had mis-extracted.)
    controls = [
        {
            "kind": "text_input",
            "label": "notice-EV92REG",
            "name": "type_id",
            "rect": {"x": 400, "y": 600},
        },
    ]
    observation = Observation(png_bytes=b"frame", source="browser", form_controls=controls)

    binding = BrowserTargetBinder().bind(_step(), observation, _decision())

    assert binding is not None
    assert binding.status == "unresolved"


def test_activate_point_on_a_different_control_is_contradicted() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[{
            "kind": "native_select",
            "label": "Visible",
            "group_field": "Visible",
            "group_id": "attributeGrid_table:1",
            "rect": {"x": 543, "y": 406},
        }],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="open the size row",
            role="prepare",
            family="activate",
            target_control="Attribute Code: size (row in grid)",
        ),
        summary="open row",
        statement_id="s3",
        execution_scope="i3:s3/statement",
        statement_kind="navigation",
    )
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=543,
        y=406,
        description="open the size row",
    ))

    binding = BrowserTargetBinder().bind(step, observation, decision)

    assert binding is not None
    assert binding.status == "contradicted"
    assert "Visible" in binding.reason


def test_exact_semantic_link_ref_becomes_bound_navigation() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        semantic_tree=[
            {
                "role": "link",
                "key": "Edit Diana Tights-28-Black",
                "url": "https://shop.test/product/1848",
                "ref": 1848,
            },
            {
                "role": "link",
                "key": "Edit Diana Tights",
                "url": "https://shop.test/product/1854",
                "ref": 1854,
            },
        ],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="Open the exact Diana Tights owner",
            role="prepare",
            family="navigate",
            target_control="Edit Diana Tights",
            target_ref="1854",
        ),
        summary="open owner",
        statement_id="s8",
        execution_scope="i7:s8/statement",
        statement_kind="navigation",
    )
    policy = BrowserActionPolicy()

    decision = policy.resolve_native_action(
        observation,
        target_control=step.action_intent.target_control,
        target_value="",
        target_ref=step.action_intent.target_ref,
        action_family=step.action_intent.family,
        instruction=step.action_intent.instruction,
    )

    assert decision is not None
    assert decision.action.action_type == "navigate"
    assert decision.action.url == "https://shop.test/product/1854"
    binding = BrowserTargetBinder().bind(step, observation, decision)
    assert binding is not None
    assert (binding.status, binding.unit_id) == ("bound", "ref:1854")


def test_exact_visible_semantic_button_ref_becomes_bound_tap() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        semantic_tree=[{
            "role": "button",
            "key": "Search",
            "ref": 42,
            "in_viewport": True,
            "point": {"x": 121.0, "y": 304.0},
        }],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="submit the filter",
            role="commit",
            family="activate",
            target_control="Search",
            target_ref="42",
        ),
        summary="submit",
        statement_id="s2",
    )
    policy = BrowserActionPolicy()

    decision = policy.resolve_native_action(
        observation,
        target_control="Search",
        target_ref="42",
        action_family="activate",
        instruction="submit the filter",
    )

    assert decision is not None
    assert (
        decision.action.action_type,
        decision.action.x,
        decision.action.y,
    ) == ("tap", 121.0, 304.0)
    binding = BrowserTargetBinder().bind(step, observation, decision)
    assert binding is not None
    assert (binding.status, binding.unit_id) == ("bound", "ref:42")


def test_semantic_binding_uses_unique_ref_not_decorated_label() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        semantic_tree=[{
            "role": "link",
            "key": "\ue608 CATALOG",
            "ref": 273644,
            "in_viewport": True,
            "point": {"x": 35.0, "y": 239.0},
        }],
    )
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="Activate Catalog",
            role="prepare",
            family="activate",
            target_control="Catalog",
            target_ref="273644",
        ),
        summary="open menu",
        statement_id="s3",
    )
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="tap",
        x=35.0,
        y=239.0,
        description="Activate Catalog",
    ))

    binding = BrowserTargetBinder().bind(step, observation, decision)

    assert binding is not None
    assert (binding.status, binding.unit_id) == ("bound", "ref:273644")


def test_exact_offscreen_semantic_target_becomes_transport_not_click() -> None:
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        semantic_tree=[{
            "role": "button",
            "key": "Add Swatch",
            "ref": 247901,
            "in_viewport": False,
            "point": {"x": 500, "y": 1450},
        }],
    )
    policy = BrowserActionPolicy()

    decision = policy.resolve_native_action(
        observation,
        target_control="Add Swatch",
        target_ref="247901",
        action_family="iterate",
        instruction="bring Add Swatch into view",
    )

    assert decision is not None
    assert decision.action.action_type == "scroll_to_ref"
    assert decision.action.target_ref == 247901


class _Future:
    def result(self):
        return None


class _Executor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *_args, **_kwargs) -> bool:
        self.calls += 1
        return True


class _BrowserBindingPolicy:
    def bind(self, step, observation, action_decision):
        return BrowserTargetBinder().bind(step, observation, action_decision)


def _run_action(tmp_path, step: SupervisorStep):
    executor = _Executor()
    result = ActionExecutor().run(
        sv_step=step,
        observation=Observation(png_bytes=b"frame", source="visual"),
        action_policy=object(),
        supervisor=object(),
        executor=executor,
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=1,
        flash=lambda _action: None,
        status=lambda _turn, _message: None,
        say=lambda _message: None,
    )
    return result, executor


def test_contradicted_activation_is_recorded_without_dispatch(tmp_path) -> None:
    step = SupervisorStep(
        action_intent=ActionIntent(
            instruction="open the size row",
            role="prepare",
            family="activate",
            target_control="Attribute Code: size (row in grid)",
        ),
        summary="open row",
        preformed_action=BrowserActionDecision(action=BrowserAction(
            action_type="tap",
            x=543,
            y=406,
            description="open the size row",
        )),
        statement_id="s3",
        execution_scope="i3:s3/statement",
        statement_kind="navigation",
    )
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        form_controls=[{
            "kind": "native_select",
            "label": "Visible",
            "group_field": "Visible",
            "group_id": "attributeGrid_table:1",
            "rect": {"x": 543, "y": 406},
        }],
    )
    executor = _Executor()

    result = ActionExecutor().run(
        sv_step=step,
        observation=observation,
        action_policy=_BrowserBindingPolicy(),
        supervisor=object(),
        executor=executor,
        prep_future=_Future(),
        log_dir=tmp_path,
        turn_no=5,
        flash=lambda _action: None,
        status=lambda _turn, _message: None,
        say=lambda _message: None,
    )

    assert result.executed is False
    assert executor.calls == 0
    assert result.binding is not None
    assert result.binding.status == "contradicted"
    turn = make_interactive_turn(
        index=5,
        observation_source="browser",
        supervisor_step=step,
        action_decision=result.action_decision,
        executed=False,
        suppressed_reason=result.suppressed_reason,
        binding=result.binding,
        statement_instance_id="i3:s3",
    )
    assert turn.action_signal is not None
    assert turn.action_signal.execution == "not_attempted"
    assert turn.action_signal.target == "off_target"


def test_write_binding_is_recorded_only_after_a_dispatchable_target(tmp_path) -> None:
    result, executor = _run_action(
        tmp_path, _step(preformed_action=_decision(), atomic_role="prepare")
    )

    assert result.executed is True
    assert executor.calls == 1
    assert result.binding is not None and result.binding.status == "bound"
    turn = make_interactive_turn(
        index=1,
        observation_source="visual",
        supervisor_step=_step(),
        action_decision=result.action_decision,
        executed=True,
        binding=result.binding,
    )
    assert turn.action_signal is not None
    assert turn.action_signal.binding == result.binding


def test_unbound_write_is_suppressed_but_acquire_action_is_not(tmp_path) -> None:
    failed, executor = _run_action(
        tmp_path,
        _step(target_control="", preformed_action=_decision()),
    )
    scroll = BaseActionDecision(action=BaseAction(
        action_type="scroll", direction="down", description="locate the target"
    ))
    acquired, acquire_executor = _run_action(
        tmp_path, _step(preformed_action=scroll)
    )

    assert failed.executed is False
    assert executor.calls == 0
    assert "target binding failed" in failed.suppressed_reason
    assert acquired.executed is True
    assert acquire_executor.calls == 1
    assert acquired.binding is None


def test_bound_write_persists_a_mutation_receipt() -> None:
    step = _step()

    turn = make_interactive_turn(
        index=1,
        observation_source="visual",
        supervisor_step=step,
        action_decision=_decision(),
        executed=True,
        binding=bind_action_target(
            binder=None,
            step=step,
            observation=Observation(png_bytes=b"frame", source="visual"),
            action_decision=_decision(),
        ),
    )

    assert turn.action_signal is not None
    receipt = turn.action_signal.mutation_receipt
    assert receipt is not None
    assert (receipt.subject_ref, receipt.field) == (
        "record:1",
        "Amount",
    )
