from __future__ import annotations

from gui_agent.core.schemas import Milestone, Observation, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.helpers import (
    _apply_required_group_checker_guard,
    target_value_state,
)
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def _check() -> _SingleCheckResult:
    return _SingleCheckResult(
        status="in_progress",
        reason="target state is not complete",
        summary="work remains",
    )


def _policy_with_plans(*plans: _PlanResult) -> tuple[MilestoneSupervisorPolicy, list[str]]:
    policy = MilestoneSupervisorPolicy()
    queue = iter(plans)
    retry_prompts: list[str] = []

    def invoke(*_args, **kwargs):
        retry_prompts.append(str(kwargs.get("extra") or ""))
        return next(queue)

    policy._invoke_planner = invoke  # type: ignore[method-assign]
    policy._is_repeated_instruction = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    return policy, retry_prompts


def test_incomplete_repeated_row_replans_commit_to_input_once():
    milestone = Milestone.model_validate({
        "id": "add-option",
        "name": "在 Size 属性中确保 Option 'XXXL' 存在并保存",
        "description": "",
        "success_condition": "Size 属性的 Options 集合中包含 'XXXL'",
        "kind": "action",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        dom_state="row-incomplete",
        form_controls=[
            {
                "label": "Admin",
                "kind": "text_input",
                "value": "",
                "required": True,
                "group_id": "option-row-1",
                "group_field": "Admin",
                "in_viewport": True,
            },
            {
                "label": "Default Store View",
                "kind": "text_input",
                "value": "XXXL",
                "group_id": "option-row-1",
                "group_field": "Default Store View",
                "in_viewport": True,
            },
        ],
    )
    policy, retry_prompts = _policy_with_plans(
        _PlanResult(
            instruction="点击 Save Attribute 保存",
            summary="save",
            atomic_role="commit",
            action_family="commit",
        ),
        _PlanResult(
            instruction="在目标选项行的 Admin 输入框填入 'XXXL'",
            summary="complete required field",
            action_family="input",
        ),
    )

    step = policy._plan_single(milestone, _check(), observation, [])

    assert step.should_act is True
    assert step.action_family == "input"
    assert "Admin" in (step.instruction or "")
    assert len(retry_prompts) == 2
    assert "结构化动作约束冲突" in retry_prompts[1]


def test_declared_partial_target_row_rejects_save_before_business_write():
    milestone = Milestone.model_validate({
        "id": "ensure-option",
        "name": "确保选项集合包含 XXXL 并保存",
        "description": "",
        "success_condition": "选项集合包含完整的 XXXL 行",
        "kind": "action",
        "mutation_mode": "ensure",
        "target_controls": ["Admin Swatch", "Admin Description"],
        "target_values": {
            "Admin Swatch": "XXXL",
            "Admin Description": "XXXL",
        },
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        dom_state="partial-target-row",
        form_controls=[
            {
                "label": "Swatch",
                "kind": "text_input",
                "value": "",
                "group_id": "collection:20",
                "group_field": "Admin",
                "in_viewport": True,
            },
            {
                "label": "Description",
                "kind": "text_input",
                "value": "XXXL",
                "group_id": "collection:20",
                "group_field": "Admin",
                "in_viewport": True,
            },
        ],
        form_controls_meta={"coverage": "partial", "truncated": True},
    )
    policy, retry_prompts = _policy_with_plans(
        _PlanResult(
            instruction="点击 Save Attribute",
            summary="persist row",
            atomic_role="commit",
            action_family="commit",
            target_control="Save Attribute",
        ),
        _PlanResult(
            instruction="在该选项行的 Admin Swatch 输入 XXXL",
            summary="complete business field",
            atomic_role="write",
            action_family="input",
            target_control="Admin Swatch",
        ),
    )

    step = policy._plan_single(milestone, _check(), observation, [])

    assert step.action_family == "input"
    assert step.target_control == "Admin Swatch"
    assert len(retry_prompts) == 2
    assert "结构化动作约束冲突" in retry_prompts[1]


def test_target_values_require_all_declared_fields_in_the_same_group():
    milestone = Milestone(
        id="ensure-option",
        name="ensure option",
        description="",
        success_condition="option row is complete",
        kind="action",
        mutation_mode="ensure",
        target_values={
            "Admin Swatch": "XXXL",
            "Admin Description": "XXXL",
        },
    )
    controls = [
        {
            "label": "Swatch",
            "kind": "text_input",
            "value": "",
            "group_id": "collection:20",
            "group_field": "Admin",
        },
        {
            "label": "Description",
            "kind": "text_input",
            "value": "XXXL",
            "group_id": "collection:20",
            "group_field": "Admin",
        },
    ]

    incomplete = target_value_state(controls, milestone)
    assert incomplete.status == "incomplete"
    assert incomplete.missing_fields == ("Admin Swatch",)

    controls[0]["value"] = "XXXL"
    complete = target_value_state(controls, milestone)
    assert complete.status == "complete"


def test_partial_inventory_absence_cannot_become_target_contradiction():
    milestone = Milestone(
        id="ensure-option",
        name="ensure option",
        description="",
        success_condition="option row is complete",
        kind="action",
        mutation_mode="ensure",
        target_values={"Admin Swatch": "XXXL"},
    )
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        form_controls=[],
        form_controls_meta={"coverage": "partial", "truncated": True},
    )
    contradicted = _SingleCheckResult(
        status="stuck",
        reason="The target field is absent.",
        summary="Cannot continue.",
        outcome_status="contradicted",
    )

    guarded = _apply_required_group_checker_guard(
        contradicted, milestone, observation
    )

    assert guarded.status == "in_progress"
    assert guarded.outcome_status == "unverified"
    assert "partial coverage" in guarded.summary


def test_visible_named_input_replans_iterate_to_input_once():
    milestone = Milestone.model_validate({
        "id": "search",
        "name": "在 Search by keyword 输入框输入 'Minerva LumaTech V-Tee'",
        "description": "",
        "success_condition": "Search by keyword current='Minerva LumaTech V-Tee'",
        "kind": "filter",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        dom_state="search-empty",
        form_controls=[{
            "label": "Search by keyword",
            "kind": "text_input",
            "value": "",
            "in_viewport": True,
        }],
    )
    policy, retry_prompts = _policy_with_plans(
        _PlanResult(
            instruction="向右滚动寻找筛选列",
            summary="search horizontally",
            direction="right",
            action_family="iterate",
        ),
        _PlanResult(
            instruction="在 Search by keyword 输入框输入 'Minerva LumaTech V-Tee'",
            summary="use visible target",
            action_family="input",
        ),
    )

    step = policy._plan_single(milestone, _check(), observation, [])

    assert step.action_family == "input"
    assert step.direction is None
    assert len(retry_prompts) == 2


def test_second_conflicting_proposal_enters_recovery_without_action():
    milestone = Milestone.model_validate({
        "id": "search",
        "name": "在 Search by keyword 输入框输入 'Minerva'",
        "description": "",
        "success_condition": "Search by keyword current='Minerva'",
        "kind": "filter",
    })
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        dom_state="search-empty",
        form_controls=[{
            "label": "Search by keyword",
            "kind": "text_input",
            "value": "",
            "in_viewport": True,
        }],
    )
    bad = _PlanResult(
        instruction="继续向右滚动",
        summary="wrong route",
        direction="right",
        action_family="iterate",
    )
    policy, _retry_prompts = _policy_with_plans(bad, bad.model_copy())
    policy._handle_stuck = lambda *_args, **_kwargs: SupervisorStep(  # type: ignore[method-assign]
        should_act=False,
        stop=False,
        goal_completed=False,
        summary="proposal rejected",
    )

    step = policy._plan_single(milestone, _check(), observation, [])

    assert step.should_act is False
    assert step.summary == "proposal rejected"
