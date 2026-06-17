from gui_agent.core.schemas import BaseAction, BaseActionDecision, Milestone, PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.policy import _type_only_search_filter_pending_submit


def _milestone(name: str, success_condition: str = "已执行搜索/筛选，界面给出响应") -> Milestone:
    return Milestone(
        id="m1",
        name=name,
        description=name,
        success_condition=success_condition,
        kind="action",
    )


def _turn(action: BaseAction, instruction: str, index: int = 1) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True,
            instruction=instruction,
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m1",
        ),
        action_decision=BaseActionDecision(action=action),
        executed=True,
    )


def test_search_filter_type_only_requires_submit() -> None:
    m = _milestone("在评论列表中搜索关键词 best")
    history = [
        _turn(
            BaseAction(action_type="type", x=1, y=1, text="best", description="输入 best"),
            "在 Review 筛选框输入 best",
        )
    ]
    assert _type_only_search_filter_pending_submit(m, history)


def test_search_filter_after_submit_does_not_block_done() -> None:
    m = _milestone("在评论列表中搜索关键词 best")
    history = [
        _turn(
            BaseAction(action_type="type", x=1, y=1, text="best", description="输入 best"),
            "在 Review 筛选框输入 best",
            index=1,
        ),
        _turn(
            BaseAction(action_type="tap", x=2, y=2, description="点击 Search"),
            "点击 Search 提交筛选",
            index=2,
        ),
    ]
    assert not _type_only_search_filter_pending_submit(m, history)


def test_plain_form_type_is_not_search_filter_guarded() -> None:
    m = _milestone("填写客户名称", success_condition="客户名称已填写")
    history = [
        _turn(
            BaseAction(action_type="type", x=1, y=1, text="Alice", description="输入 Alice"),
            "在客户名称输入框输入 Alice",
        )
    ]
    assert not _type_only_search_filter_pending_submit(m, history)
