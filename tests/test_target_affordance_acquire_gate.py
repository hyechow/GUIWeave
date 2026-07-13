from __future__ import annotations

import gui_agent.core.supervisor.milestone.llm_runtime as policy_mod
import gui_agent.core.supervisor.milestone.policy as supervisor_policy_mod
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.acquisition import TargetAcquireController


class _CheckerReached(Exception):
    pass


def _acquire_plan(controls: list[dict], milestone: Milestone):
    controller = TargetAcquireController()
    return controller.decide(controls, milestone, scope=milestone.id).plan


def _notify_milestone() -> Milestone:
    return Milestone(
        id="m_notify",
        name=(
            "在 'Notes for this Order' 表单的 Comment 栏填入 'sorry we are bankrupt'，"
            "勾选 'Notify Customer by Email'，点击 Update"
        ),
        description=(
            "在 'Notes for this Order' 表单的 Comment 栏填入 'sorry we are bankrupt'，"
            "勾选 'Notify Customer by Email'，点击 Update"
        ),
        success_condition="页面显示成功提示或评论历史中新增了对应备注",
        kind="action",
        target_controls=["Comment", "Notify Customer by Email"],
    )


def _offscreen_controls() -> list[dict]:
    return [
        {
            "kind": "native_select",
            "label": "Status",
            "rect": {"x": 349, "y": 1558, "w": 200, "h": 32},
            "in_viewport": False,
            "viewport_pos": "below",
        },
        {
            "kind": "textarea",
            "label": "Comment",
            "rect": {"x": 467, "y": 1646, "w": 400, "h": 120},
            "in_viewport": False,
            "viewport_pos": "below",
        },
        {
            "kind": "checkbox_input",
            "label": "Notify Customer by Email",
            "value": "off",
            "rect": {"x": 321, "y": 1714, "w": 24, "h": 24},
            "in_viewport": False,
            "viewport_pos": "below",
        },
    ]


def test_target_acquire_uses_offscreen_structured_controls() -> None:
    plan = _acquire_plan(_offscreen_controls(), _notify_milestone())

    assert plan is not None
    assert plan.direction == "down"
    assert "Comment" in plan.instruction
    assert "滚动" in plan.instruction
    assert "AcquireTarget" in plan.summary


def test_policy_acquire_gate_bypasses_checker_for_known_offscreen_controls(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)

    policy = supervisor_policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(_notify_milestone())
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="browser",
        url="http://example.test/admin/sales/order/view/order_id/65/",
        form_controls=_offscreen_controls(),
        dom_state="order-detail-controls-below",
    )

    step = policy.step(obs, goal="notify customer", history=[])

    assert checker_calls == []
    assert step.should_act is True
    assert step.atomic_role == "iterate"
    assert step.direction == "down"
    assert step.instruction and "Comment" in step.instruction
    assert "Edit" not in step.instruction


def test_target_acquire_continues_until_all_targets_are_visible() -> None:
    controls = _offscreen_controls()
    controls[1] = {
        **controls[1],
        "in_viewport": True,
        "viewport_pos": "in",
        "rect": {"x": 467, "y": 646, "w": 400, "h": 120},
    }

    plan = _acquire_plan(controls, _notify_milestone())

    assert plan is not None
    assert plan.direction == "down"
    assert "Notify Customer by Email" in plan.instruction


def test_target_acquire_stays_out_when_all_target_controls_are_visible() -> None:
    controls = _offscreen_controls()
    controls[1] = {
        **controls[1],
        "in_viewport": True,
        "viewport_pos": "in",
        "rect": {"x": 467, "y": 646, "w": 400, "h": 120},
    }
    controls[2] = {
        **controls[2],
        "in_viewport": True,
        "viewport_pos": "in",
        "rect": {"x": 321, "y": 714, "w": 24, "h": 24},
    }

    assert _acquire_plan(controls, _notify_milestone()) is None


def test_target_acquire_scrolls_to_offscreen_rich_editor_after_section_expanded() -> None:
    controls = [
        {
            "kind": "section_toggle",
            "label": "Content",
            "value": "true",
            "rect": {"x": 528, "y": 931, "w": 1118, "h": 62},
        },
        {
            "kind": "rich_textarea",
            "label": "Short Description",
            "id": "product_form_short_description_ifr",
            "in_viewport": False,
            "viewport_pos": "below",
            "rect": {"x": 528, "y": 1335, "w": 542, "h": 402},
        },
    ]

    plan = _acquire_plan(controls, _description_milestone())

    assert plan is not None
    assert plan.direction == "down"
    assert "Short Description" in plan.instruction


def _return_location_milestone() -> Milestone:
    # After a terminal Submit succeeded and redirected, the return-contract recovery re-opens the
    # milestone to locate the return field, leaking the field name into the milestone text.
    return Milestone(
        id="m_submit",
        name="点 Submit Shipment（继续定位返回字段：submit_status）",
        description="提交发货单后读取 submit_status",
        success_condition="发货已保存",
        kind="action",
    )


def _order_status_control() -> list[dict]:
    # The order/comment "Status" dropdown, offscreen. Its label "Status" is a SUBSTRING of the
    # milestone's return-field token "submit_status" — must NOT be treated as a named target.
    return [
        {
            "kind": "native_select",
            "label": "Status",
            "rect": {"x": 349, "y": 1558, "w": 200, "h": 32},
            "in_viewport": False,
            "viewport_pos": "below",
        },
    ]


def test_acquire_gate_ignores_status_substring_of_submit_status() -> None:
    # Regression for WebArena 499 (20260708_165316): AcquireGate matched "Status" against the
    # return-field token "submit_status" and scrolled to the order Status dropdown, amplifying a
    # return-contract violation into a stuck loop. Word-boundary matching must reject it.
    plan = _acquire_plan(_order_status_control(), _return_location_milestone())
    assert plan is None


def test_acquire_gate_still_matches_standalone_status_control() -> None:
    # But a milestone that genuinely targets a standalone "Status" control still acquires it.
    ms = Milestone(
        id="m_status",
        name="将 Status 下拉设为 Processing",
        description="",
        success_condition="Status 已设为 Processing",
        kind="action",
        target_controls=["Status"],
    )
    plan = _acquire_plan(_order_status_control(), ms)
    assert plan is not None
    assert plan.direction == "down"


def _description_milestone() -> Milestone:
    return Milestone(
        id="m_description",
        name="在 Content 区将 Short Description 字段更新为 3 customer(s) love it! 并保存",
        description="",
        success_condition="页面显示保存成功提示或返回列表",
        kind="action",
        target_controls=["Content", "Short Description"],
    )


def _description_without_section_milestone() -> Milestone:
    return Milestone(
        id="m_description",
        name="将 Short Description 更新为 3 customer(s) love it! 并保存",
        description="",
        success_condition="页面显示保存成功提示或返回列表",
        kind="action",
        target_controls=["Short Description"],
    )


def test_target_acquire_expands_named_visible_section() -> None:
    plan = _acquire_plan(
        [{
            "kind": "section_toggle",
            "label": "Content",
            "value": "false",
            "rect": {"x": 525, "y": 786, "w": 1000, "h": 40},
        }],
        _description_milestone(),
    )

    assert plan is not None
    assert "Content" in plan.instruction
    assert "展开" in plan.instruction
    assert plan.direction is None


def test_target_acquire_skips_named_section_with_unknown_state() -> None:
    # Review M1/M2: a section whose expanded-state is UNKNOWN ('' from form_reader — a custom
    # accordion with no aria-expanded/recognizable class) must NOT be clicked. Clicking a possibly
    # already-open section collapses it and hides the target, and re-firing every turn on an
    # unrecognized state is a toggle loop. Only an explicitly-collapsed section (value 'false',
    # covered by the test above) is clicked.
    plan = _acquire_plan(
        [{
            "kind": "section_toggle",
            "label": "Content",
            # no value/selected_text → expanded-state unknown
            "rect": {"x": 525, "y": 786, "w": 1000, "h": 40},
        }],
        _description_milestone(),
    )

    assert plan is None


def test_target_acquire_stays_out_when_target_editor_visible() -> None:
    plan = _acquire_plan(
        [
            {
                "kind": "section_toggle",
                "label": "Content",
                "value": "false",
                "rect": {"x": 525, "y": 586, "w": 1000, "h": 40},
            },
            {
                "kind": "rich_textarea",
                "label": "Short Description",
                "value": "",
                "in_viewport": True,
                "viewport_pos": "in",
                "rect": {"x": 530, "y": 760, "w": 540, "h": 260},
            },
        ],
        _description_milestone(),
    )

    assert plan is None


def test_target_acquire_does_not_guess_undeclared_section() -> None:
    plan = _acquire_plan(
        [{
            "kind": "section_toggle",
            "label": "Content",
            "value": "false",
            "rect": {"x": 525, "y": 786, "w": 1000, "h": 40},
        }],
        _description_without_section_milestone(),
    )

    assert plan is None


def test_target_acquire_does_not_guess_among_multiple_sections() -> None:
    plan = _acquire_plan(
        [
            {
                "kind": "section_toggle",
                "label": "Content",
                "value": "false",
                "rect": {"x": 525, "y": 786, "w": 1000, "h": 40},
            },
            {
                "kind": "section_toggle",
                "label": "Advanced Settings",
                "value": "false",
                "rect": {"x": 525, "y": 830, "w": 1000, "h": 40},
            },
        ],
        _description_without_section_milestone(),
    )

    assert plan is None


def test_target_acquire_scrolls_to_named_offscreen_section() -> None:
    plan = _acquire_plan(
        [{
            "kind": "section_toggle",
            "label": "Content",
            "value": "false",
            "in_viewport": False,
            "viewport_pos": "below",
            "rect": {"x": 525, "y": 1300, "w": 1000, "h": 40},
        }],
        _description_milestone(),
    )

    assert plan is not None
    assert plan.direction == "down"
    assert "Content" in plan.instruction


def test_target_acquire_scrolls_to_expanded_offscreen_section() -> None:
    plan = _acquire_plan(
        [{
            "kind": "section_toggle",
            "label": "Content",
            "value": "true",
            "in_viewport": False,
            "viewport_pos": "above",
            "rect": {"x": 525, "y": -120, "w": 1000, "h": 40},
        }],
        _description_milestone(),
    )

    assert plan is not None
    assert plan.direction == "up"
    assert "Content" in plan.instruction


def test_named_section_below_wins_over_visible_same_name_fields() -> None:
    plan = _acquire_plan(
        [
            {
                "kind": "section_toggle",
                "label": "Configurations",
                "value": "true",
                "in_viewport": False,
                "viewport_pos": "below",
                "rect": {"x": 528, "y": 2021},
            },
            {
                "kind": "native_select",
                "label": "Size",
                "selected_text": "",
                "options": ["XS", "XXXL"],
                "rect": {"x": 356, "y": 834},
            },
        ],
        Milestone(
            id="m-config",
            name="在 Configurations 区域生成 green + XXXL 组合并保存",
            description="在 Configurations 区域生成 green + XXXL 组合并保存",
            success_condition="Configurations 集合包含 green + XXXL 组合",
            kind="action",
            target_controls=["Configurations", "Size"],
        ),
    )

    assert plan is not None
    assert plan.direction == "down"
    assert "Configurations" in plan.instruction


def test_policy_named_section_precedes_flat_target_affordance(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)

    milestone = Milestone(
        id="m-config",
        name="在 Configurations 区域生成 green + XXXL 组合并保存",
        description="在 Configurations 区域生成 green + XXXL 组合并保存",
        success_condition="Configurations 集合包含 green + XXXL 组合",
        kind="action",
        target_controls=["Configurations", "Color"],
    )
    policy = supervisor_policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(milestone)
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="browser",
        url="http://example.test/admin/catalog/product/edit/id/1492/",
        form_controls=[
            {
                "kind": "section_toggle",
                "label": "Configurations",
                "value": "true",
                "in_viewport": False,
                "viewport_pos": "below",
                "rect": {"x": 528, "y": 2021},
            },
            {
                "kind": "native_select",
                "label": "Color",
                "selected_text": "",
                "options": ["Green"],
                "in_viewport": False,
                "viewport_pos": "below",
                "rect": {"x": 356, "y": 1495},
            },
        ],
        dom_state="product-parent-fields",
    )

    step = policy.step(obs, goal="create variation", history=[])

    assert checker_calls == []
    assert step.should_act is True
    assert step.direction == "down"
    assert step.instruction and "Configurations" in step.instruction
    assert "Color" not in step.instruction


def test_policy_collapsed_section_is_acquired_before_checker(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)

    policy = supervisor_policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(_description_milestone())
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="browser",
        url="http://example.test/admin/catalog/product/edit/id/1108/",
        form_controls=[{
            "kind": "section_toggle",
            "label": "Content",
            "value": "false",
            "rect": {"x": 525, "y": 786, "w": 1000, "h": 40},
        }],
        dom_state="product-edit-content-collapsed",
    )

    step = policy.step(obs, goal="update short description", history=[])

    assert checker_calls == []
    assert step.should_act is True
    assert step.target_control == "Content"
    assert step.action_family == "activate"


def test_semantic_collection_query_resolves_unique_visual_section() -> None:
    milestone = Milestone(
        id="m-config",
        name="在配置集合中添加组合",
        description="",
        success_condition="组合已保存",
        kind="action",
        target_controls=["configurations_collection"],
        target_values={"Color": "green", "Size": "XXXL"},
    )
    decision = TargetAcquireController().decide(
        [
            {
                "kind": "native_select",
                "label": "Color",
                "in_viewport": True,
                "rect": {"y": 700},
            },
            {
                "kind": "native_select",
                "label": "Size",
                "in_viewport": True,
                "rect": {"y": 780},
            },
            {
                "kind": "section_toggle",
                "label": "Configurations",
                "in_viewport": False,
                "viewport_pos": "below",
                "rect": {"y": 2021},
            },
        ],
        milestone,
        scope=milestone.id,
    )

    assert decision.status == "act"
    assert decision.plan is not None
    assert decision.target_labels == ("Configurations",)
    assert decision.plan.direction == "down"


def test_target_values_do_not_participate_in_position_binding() -> None:
    milestone = Milestone(
        id="m-config",
        name="更新配置",
        description="",
        success_condition="已更新",
        kind="action",
        target_controls=["configurations_collection"],
        target_values={"Color": "green", "Size": "XXXL"},
    )
    decision = TargetAcquireController().decide(
        [
            {"kind": "native_select", "label": "Color", "in_viewport": True},
            {"kind": "native_select", "label": "Size", "in_viewport": True},
        ],
        milestone,
        scope=milestone.id,
    )

    assert decision.status == "inactive"


def test_target_probe_refuses_equal_semantic_matches() -> None:
    milestone = Milestone(
        id="m-ambiguous",
        name="更新备注",
        description="",
        success_condition="已更新",
        kind="action",
        target_controls=["Comment"],
    )
    decision = TargetAcquireController().decide(
        [
            {"kind": "textarea", "label": "Comment", "in_viewport": False, "viewport_pos": "below"},
            {"kind": "textarea", "label": "Comment", "in_viewport": False, "viewport_pos": "above"},
        ],
        milestone,
        scope=milestone.id,
    )

    assert decision.status == "ambiguous"


def test_ambiguous_optional_structure_falls_back_to_visual_checker(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(supervisor_policy_mod, "is_loading_frame", lambda _obs: False)
    milestone = Milestone(
        id="m-ambiguous",
        name="更新备注",
        description="",
        success_condition="已更新",
        kind="action",
        target_controls=["Comment"],
    )
    policy = supervisor_policy_mod.MilestoneSupervisorPolicy()
    policy.reseed(milestone)
    observation = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="browser",
        form_controls=[
            {"kind": "textarea", "label": "Comment"},
            {"kind": "textarea", "label": "Comment"},
        ],
    )

    try:
        policy.step(observation, goal="update comment", history=[])
    except _CheckerReached:
        pass

    assert checker_calls == [1]


def test_acquire_session_tracks_geometry_progress_and_exhausts_no_progress() -> None:
    milestone = Milestone(
        id="m-config",
        name="更新配置",
        description="",
        success_condition="已更新",
        kind="action",
        target_controls=["configurations_collection"],
    )
    controller = TargetAcquireController()

    def controls(y: int, *, visible: bool = False) -> list[dict]:
        return [{
            "kind": "section_toggle",
            "label": "Configurations",
            "in_viewport": visible,
            "viewport_pos": "in" if visible else "below",
            "rect": {"y": y},
        }]

    assert controller.decide(controls(2644), milestone, scope="row:1").status == "act"
    assert controller.decide(controls(2021), milestone, scope="row:1").status == "act"
    assert controller.decide(controls(2021), milestone, scope="row:1").status == "act"
    assert controller.decide(controls(2021), milestone, scope="row:1").status == "exhausted"

    other = controller.decide(controls(1500), milestone, scope="row:2")
    assert other.status == "act"
    ready = controller.decide(controls(700, visible=True), milestone, scope="row:2")
    assert ready.status == "ready"

    assert controller.decide(controls(1000), milestone, scope="row:3").status == "act"
    assert controller.decide(controls(1100), milestone, scope="row:3").status == "act"
    assert controller.decide(controls(1200), milestone, scope="row:3").status == "exhausted"
