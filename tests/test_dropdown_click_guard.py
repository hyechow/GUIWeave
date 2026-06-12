"""_repeated_candidate_click：搜索式下拉「重选自证循环」的确定性断路器（回归 20260612_184401）。

点击候选后列表收起、输入框保留所选文本（仍带搜索图标）；checker 偶发把这个状态误判为
「尚未选定」，planner 服从 missing_evidence 再点一次 → 把列表重新点开 → 误判变成现实。
闸的签名：同一 milestone 内已执行过「点击候选 X」、其后没有对 X 的重新输入，又要点 X。
"""

from __future__ import annotations

from gui_agent.core.schemas import PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.helpers import _repeated_candidate_click

MS = "m2"
X = "交管测试专用地图_1楼"
CLICK_X = f"点击预设地图候选列表中的「{X}」选项"
TYPE_X = f"在「预设地图」搜索框中输入「{X}」"


def _turn(idx: int, instruction: str, *, milestone_id: str = MS, executed: bool = True) -> PolicyTurn:
    return PolicyTurn(
        index=idx,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True, instruction=instruction, stop=False,
            goal_completed=False, summary="", milestone_id=milestone_id,
        ),
        executed=executed,
    )


def test_repeat_click_after_executed_click_triggers():
    history = [_turn(6, TYPE_X), _turn(7, CLICK_X)]
    assert _repeated_candidate_click(CLICK_X, MS, history) == X


def test_first_click_does_not_trigger():
    history = [_turn(6, TYPE_X)]
    assert _repeated_candidate_click(CLICK_X, MS, history) is None


def test_retype_between_clicks_legitimizes_followup():
    # 重新输入会把候选列表重新打开——其后的点击是合法的，不应拦
    history = [_turn(6, TYPE_X), _turn(7, CLICK_X), _turn(8, TYPE_X)]
    assert _repeated_candidate_click(CLICK_X, MS, history) is None


def test_different_option_does_not_trigger():
    history = [_turn(7, CLICK_X)]
    other = "点击预设区域候选列表中的「禁止停止区_区域14」选项"
    assert _repeated_candidate_click(other, MS, history) is None


def test_unexecuted_prior_click_does_not_trigger():
    history = [_turn(7, CLICK_X, executed=False)]
    assert _repeated_candidate_click(CLICK_X, MS, history) is None


def test_other_milestone_click_does_not_trigger():
    history = [_turn(7, CLICK_X, milestone_id="m3")]
    assert _repeated_candidate_click(CLICK_X, MS, history) is None


def test_non_click_instruction_does_not_trigger():
    history = [_turn(7, CLICK_X)]
    assert _repeated_candidate_click(TYPE_X, MS, history) is None
