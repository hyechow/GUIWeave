"""_repeated_candidate_click：搜索式下拉「重选自证循环」的确定性断路器（回归 20260612_184401）。

点击候选后列表收起、输入框保留所选文本（仍带搜索图标）；checker 偶发把这个状态误判为
「尚未选定」，planner 服从 missing_evidence 再点一次 → 把列表重新点开 → 误判变成现实。
闸的签名：同一 milestone 内已执行过「点击候选 X」、其后没有对 X 的重新输入，又要点 X。
"""

from __future__ import annotations

from gui_agent.core.schemas import PolicyTurn, SupervisorStep
from gui_agent.core.supervisor.milestone.helpers import _reopens_selected_dropdown, _repeated_candidate_click

MS = "m2"
X = "交管测试专用地图_1楼"
CLICK_X = f"点击预设地图候选列表中的「{X}」选项"
TYPE_X = f"在「预设地图」搜索框中输入「{X}」"
CLICK_SAME_VALUE_OTHER_FIELD = f"点击目标站点候选列表中的「{X}」选项"
TYPE_SAME_VALUE_OTHER_FIELD = f"在「目标站点」搜索框中输入「{X}」"
REOPEN_FIELD = "点击预设地图输入框以展开候选列表"
REOPEN_OTHER_FIELD = "点击目标站点输入框以展开候选列表"


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


def test_same_option_in_different_field_does_not_trigger():
    history = [_turn(7, CLICK_X)]
    assert _repeated_candidate_click(CLICK_SAME_VALUE_OTHER_FIELD, MS, history) is None


def test_generic_field_context_does_not_trigger():
    history = [_turn(7, CLICK_X)]
    generic = f"点击候选列表中的「{X}」选项"
    assert _repeated_candidate_click(generic, MS, history) is None


# ── 尾部字段 context：「点击候选『X』以选择<字段>」（多选 chip 重点击=取消选中,回归 20260612_205558）──

CLICK_TRAILING = "点击候选列表中的「testgroup」以选择机器人群组"


def test_trailing_field_context_triggers():
    history = [_turn(9, CLICK_TRAILING)]
    assert _repeated_candidate_click(CLICK_TRAILING, MS, history) == "testgroup"


def test_trailing_context_different_field_does_not_trigger():
    history = [_turn(9, CLICK_TRAILING)]
    other_field = "点击候选列表中的「testgroup」以选择目标站点"
    assert _repeated_candidate_click(other_field, MS, history) is None


def test_trailing_pure_verb_is_not_context():
    # 「以完成选定」是纯动词尾巴，不构成字段 context → 仍按泛指处理（不触发）
    instr = "点击候选列表中的「testgroup」选项以完成选定"
    history = [_turn(9, instr)]
    assert _repeated_candidate_click(instr, MS, history) is None


def test_retype_same_value_in_different_field_does_not_legitimize_original_repeat():
    history = [_turn(7, CLICK_X), _turn(8, TYPE_SAME_VALUE_OTHER_FIELD)]
    assert _repeated_candidate_click(CLICK_X, MS, history) == X


def test_unexecuted_prior_click_does_not_trigger():
    history = [_turn(7, CLICK_X, executed=False)]
    assert _repeated_candidate_click(CLICK_X, MS, history) is None


def test_other_milestone_click_does_not_trigger():
    history = [_turn(7, CLICK_X, milestone_id="m3")]
    assert _repeated_candidate_click(CLICK_X, MS, history) is None


def test_non_click_instruction_does_not_trigger():
    history = [_turn(7, CLICK_X)]
    assert _repeated_candidate_click(TYPE_X, MS, history) is None


def test_reopen_selected_dropdown_triggers():
    history = [_turn(6, TYPE_X), _turn(7, CLICK_X)]
    assert _reopens_selected_dropdown(REOPEN_FIELD, MS, history) == ("预设地图", X)


def test_reopen_different_field_does_not_trigger():
    history = [_turn(6, TYPE_X), _turn(7, CLICK_X)]
    assert _reopens_selected_dropdown(REOPEN_OTHER_FIELD, MS, history) is None


def test_retype_before_reopen_legitimizes_dropdown_reopen():
    history = [_turn(6, TYPE_X), _turn(7, CLICK_X), _turn(8, TYPE_X)]
    assert _reopens_selected_dropdown(REOPEN_FIELD, MS, history) is None
