from __future__ import annotations

from gui_agent.core.schemas import Milestone, Observation, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
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

