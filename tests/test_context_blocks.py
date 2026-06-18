from __future__ import annotations

from gui_agent.context import ContextBlock, ContextBudgeter, ContextBundle
from gui_agent.context.runtime import (
    form_controls_block,
    history_block,
    render_prompt_context,
)
from gui_agent.core.schemas import PolicyTurn, SupervisorStep


def _blk(id_: str, budget: str, chars: int, *, ttl: str = "turn") -> ContextBlock:
    return ContextBlock(id_, "runtime_state", "test", "x" * chars, ttl=ttl, budget=budget)


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


def test_budgeter_keeps_all_when_under_ceiling():
    blocks = [_blk("a", "low", 100), _blk("b", "high", 100)]
    result = ContextBudgeter(max_chars=10_000).apply(blocks)
    assert not result.dropped
    assert {b.id for b in result.kept} == {"a", "b"}


def test_budgeter_drops_lowest_tier_first_and_never_required():
    # required(800) + high(800) + medium(800) + low(800) = ~3200 over a 2000 ceiling.
    # Shed low, then medium, until it fits; required + high survive.
    blocks = [
        _blk("req", "required", 800),
        _blk("hi", "high", 800),
        _blk("med", "medium", 800),
        _blk("lo", "low", 800),
    ]
    result = ContextBudgeter(max_chars=2000).apply(blocks)
    dropped = {b.id for b in result.dropped}
    kept = {b.id for b in result.kept}
    assert "req" in kept and "hi" in kept       # required never dropped; high outranks low/medium
    assert dropped == {"lo", "med"}              # lowest tiers shed first, in order
    assert not result.over_budget


def test_budgeter_preserves_render_order_of_kept_blocks():
    blocks = [_blk("first", "high", 50), _blk("mid", "low", 4000), _blk("last", "high", 50)]
    text = ContextBudgeter(max_chars=500).apply(blocks).text
    assert "context: mid" not in text                       # the big low-tier block was dropped
    assert text.index("context: first") < text.index("context: last")  # order preserved


def test_budgeter_within_tier_keeps_live_turn_over_stale_session():
    # Two medium blocks, only one can stay. The STALE session-scoped block is shed first; the
    # current turn's live observation is kept (recency wins within a tier).
    blocks = [
        _blk("req", "required", 1300),
        _blk("turn_med", "medium", 350, ttl="turn"),
        _blk("session_med", "medium", 350, ttl="session"),
    ]
    result = ContextBudgeter(max_chars=2000).apply(blocks)
    assert {b.id for b in result.dropped} == {"session_med"}


def test_budgeter_required_over_ceiling_keeps_required_and_flags_over_budget():
    blocks = [_blk("req", "required", 5000), _blk("lo", "low", 100)]
    result = ContextBudgeter(max_chars=1000).apply(blocks)
    assert {b.id for b in result.kept} == {"req"}   # required survives even alone over budget
    assert {b.id for b in result.dropped} == {"lo"}
    assert result.over_budget is True


def test_budgeter_long_files_knowledge_history_keep_required_and_report_reasons():
    blocks = [
        ContextBlock(
            "runtime.task.file_refs",
            "file_reference",
            "goal_at_refs",
            "f" * 2200,
            priority=20,
            ttl="task",
            budget="required",
        ),
        ContextBlock(
            "knowledge.section.orders",
            "knowledge_section",
            "knowledge/browser/shopping_admin/Orders.md",
            "k" * 1800,
            priority=50,
            ttl="session",
            budget="high",
        ),
        ContextBlock(
            "runtime.history.recent_actions",
            "runtime_state",
            "policy_history",
            "h" * 1800,
            priority=80,
            ttl="session",
            budget="low",
        ),
    ]

    result = ContextBudgeter(max_chars=4700).apply(blocks)
    report = result.to_report(label="planner.knowledge")

    kept = {b.id for b in result.kept}
    dropped = {b.id for b in result.dropped}
    assert "runtime.task.file_refs" in kept
    assert "knowledge.section.orders" in kept
    assert dropped == {"runtime.history.recent_actions"}
    assert report["included_count"] == 2
    assert report["dropped_count"] == 1
    file_row = next(b for b in report["blocks"] if b["id"] == "runtime.task.file_refs")
    hist_row = next(b for b in report["blocks"] if b["id"] == "runtime.history.recent_actions")
    assert file_row["source"] == "goal_at_refs"
    assert file_row["priority"] == 20
    assert file_row["ttl"] == "task"
    assert file_row["truncation_reason"] == "not_truncated"
    assert hist_row["included"] is False
    assert hist_row["truncation_reason"] == "dropped_over_budget"


def test_render_prompt_context_enforces_ceiling_and_logs_drops():
    logs: list[str] = []
    reports: list[dict] = []
    blocks = [_blk("keep", "required", 100), _blk("drop", "low", 4000)]
    text = render_prompt_context(
        blocks,
        max_chars=500,
        label="checker",
        say=logs.append,
        report_sink=reports,
    )
    assert "context: keep" in text and "context: drop" not in text
    assert any("ContextBudget" in line and "checker" in line and "drop" in line for line in logs)
    assert reports[0]["label"] == "checker"
    assert reports[0]["included"][0]["id"] == "keep"
    assert reports[0]["dropped"][0]["id"] == "drop"


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
