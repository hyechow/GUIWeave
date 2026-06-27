from gui_agent.prompts import load_prompt_text


def test_android_planner_treats_type_as_single_input_action():
    prompt = load_prompt_text("task.milestone.android.planner")

    assert "type 是一个单动作" in prompt
    assert "禁止先只输出「点击<输入框>」" in prompt
