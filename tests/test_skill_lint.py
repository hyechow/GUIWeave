"""validate_skill_doc keeps _skill.md pure orchestration so it never grows into a second
manual: steps stay short, name no UI/HOW action, and a skill carries only 触发/数据/步骤.
"""

from __future__ import annotations

from gui_agent.core.self_learning.app_summary import validate_skill_doc

CLEAN = """\
# 编排技能
> 前言：不写界面操作。
## skill: 机器人移动联调实验
- 触发: 双车实验 / 联调实验
- 数据角色（取自 @配置，每台机器人一组）:
  - 预设站点 = 放置位置 ＋ 连通检测起点
  - 目标站点 = 连通检测终点 ＋ 移动订单终点
- 步骤:
  1. 按「新建虚拟机器人配置」新建并启用全部机器人
  2. 把每台摆到它的「预设站点」
  3. 检测每台「预设站点 → 目标站点」连通并记录判定
  4. 为每台建一个到它「目标站点」的移动订单
  5. 读取并记录全部订单的执行状态
"""


def test_clean_skill_has_no_issues():
    assert validate_skill_doc(CLEAN) == []


def test_step_with_ui_action_flagged():
    doc = (
        "## skill: x\n- 触发: t\n- 步骤:\n"
        "  1. 点击「工具」菜单进入虚拟机器人页\n"
    )
    issues = validate_skill_doc(doc)
    assert any("界面操作词" in i for i in issues)
    # both UI tokens surfaced
    assert any("点击" in i and "菜单" in i for i in issues)


def test_overlong_step_flagged():
    long_step = (
        "把每台机器人依次摆到它各自的预设站点然后再逐一核对每台位置是否完全正确无误并确认"
        "状态都正常之后才继续进行下一步操作流程不要遗漏任何一台机器人"
    )
    assert len(long_step) > 50  # guard the fixture itself
    doc = f"## skill: x\n- 触发: t\n- 步骤:\n  1. {long_step}\n"
    assert any("过长" in i for i in validate_skill_doc(doc))


def test_illegal_field_flagged():
    doc = "## skill: x\n- 触发: t\n- 操作说明: 先打开页面\n- 步骤:\n  1. 建车\n"
    assert any("非法字段" in i and "操作说明" in i for i in validate_skill_doc(doc))


def test_data_role_sublines_and_preamble_not_misparsed():
    # nested "- 预设站点 = ..." (no colon) and the preamble blockquote must not trip the lint
    assert validate_skill_doc(CLEAN) == []
