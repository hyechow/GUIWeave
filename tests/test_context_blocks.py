from __future__ import annotations

from gui_agent.context import ContextBlock, ContextBundle
from gui_agent.context.runtime import (
    form_controls_block,
    history_block,
    render_prompt_context,
)
from gui_agent.core.schemas import PolicyTurn, SupervisorStep


def test_context_block_renders_source_metadata():
    block = ContextBlock(
        id="runtime.checker_result",
        source_type="runtime_state",
        source="checker",
        ttl="turn",
        content="status=in_progress",
        metadata={"milestone": "m1"},
    )

    text = block.render()

    assert "[context: runtime.checker_result" in text
    assert "type=runtime_state" in text
    assert "source=checker" in text
    assert "milestone=m1" in text
    assert "status=in_progress" in text


def test_context_bundle_preserves_order_by_default_and_can_sort_by_priority():
    high = ContextBlock("b", "runtime_state", "test", "B", priority=10)
    low = ContextBlock("a", "runtime_state", "test", "A", priority=50)
    bundle = ContextBundle((low, high))

    default_text = bundle.render()
    sorted_text = bundle.render(sort_by_priority=True)

    assert default_text.index("context: a") < default_text.index("context: b")
    assert sorted_text.index("context: b") < sorted_text.index("context: a")


def test_runtime_history_context_keeps_existing_history_text_with_metadata():
    turn = PolicyTurn(
        index=1,
        observation_source="browser",
        supervisor=SupervisorStep(
            should_act=True,
            instruction="点击搜索框",
            stop=False,
            goal_completed=False,
            summary="需要搜索",
        ),
        executed=False,
    )

    text = history_block([turn]).render()

    assert "context: runtime.history.recent_actions" in text
    assert "ttl=session" in text
    assert "需要搜索" in text
    assert "结果尚未记录" in text


def test_form_controls_context_marks_adapter_source():
    block = form_controls_block([{
        "kind": "native_select",
        "label": "Status",
        "options": ["Canceled", "Complete"],
    }])

    assert block is not None
    text = render_prompt_context([block])
    assert "context: runtime.observation.form_controls" in text
    assert "source=platform_adapter" in text
    assert "浏览器 DOM 表单控件" in text
    assert "Status: native_select" in text
