from gui_agent.reports.builder import _group_steps_by_milestone
from gui_agent.reports.models import ReportStep
from gui_agent.reports.orchestrator_html import _render_program_section
from gui_agent.reports.prompt_html import _render_module_io_html
from gui_agent.reports.runner_html import _render_thumb_time, _turn_elapsed_seconds


def test_module_io_renders_summary_collapsed_schema_and_tokens():
    html = _render_module_io_html([
        {
            "kind": "prompt_snapshot",
            "label": "checker",
            "roles": [
                {"role": "system", "parts": [
                    {
                        "label": "task_prompt",
                        "text": "TASK",
                        "chars": 4,
                    },
                    {
                        "label": "schema_instruction",
                        "text": "顶层必填字段：status, reason\n顶层可选字段：summary",
                        "chars": 32,
                    },
                ]},
            ],
        },
        {
            "kind": "llm_output",
            "label": "checker",
            "schema": "_SingleCheckResult",
            "raw_output": '{"status":"done","reason":"ok"}',
            "parsed": {"status": "done", "reason": "ok"},
        },
    ], {"checker": {"input": 4108, "output": 73}})

    assert "prompt-call-summary prompt-call-summary-ok" in html
    assert "done · ok" in html
    assert "prompt-detail-meta" in html
    assert "1 call · 4.1k/73 tok" in html
    assert "prompt-token-detail" in html
    assert "Token 明细" in html
    assert "prompt-schema" in html
    assert "prompt-part-collapsed" in html
    assert "schema_instruction · _SingleCheckResult · 2 required / 1 optional" in html
    assert "prompt-token-total" not in html
    assert "checker" in html


def test_module_io_surfaces_selector_reports_from_legacy_logs():
    html = _render_module_io_html([
        {
            "kind": "prompt_snapshot",
            "label": "checker",
            "roles": [{"role": "system", "parts": [{"label": "task_prompt", "text": "TASK"}]}],
        },
        {
            "kind": "llm_output",
            "label": "checker",
            "schema": "_SingleCheckResult",
            "raw_output": '{"status":"in_progress","reason":"need nav"}',
            "parsed": {"status": "in_progress", "reason": "need nav"},
        },
        {
            "kind": "selector",
            "label": "knowledge.selector",
            "cache": "miss",
            "page_identity": "Marketing > All Reviews",
            "page_known": True,
            "section_ids": ["s13", "s25"],
            "sections": ["Moderate_product_reviews", "The_Admin_sidebar"],
            "fallback_triggered": False,
            "cached": True,
            "reason": "当前页面需要评论管理和侧边栏导航知识",
        },
        {
            "kind": "prompt_snapshot",
            "label": "planner",
            "roles": [{"role": "system", "parts": [{"label": "task_prompt", "text": "PLAN"}]}],
        },
    ], {
        "checker": {"input": 4108, "output": 73},
        "selector": {"input": 1312, "output": 53},
        "planner": {"input": 8120, "output": 111},
    })

    assert "3 calls · 13.5k/237 tok" in html
    assert "2. selector" in html
    assert "cache=miss · Moderate_product_reviews, The_Admin_sidebar" in html
    assert "_SelectorResult" in html
    assert "selector_report" in html
    assert "旧日志没有记录该模块原始输出" not in html


def test_orchestrator_program_renders_foreach_block_and_body():
    # A foreach program must render its loop block + indented body (not be silently dropped), with the
    # legacy list_read badge (still rendered for old logs) and the accumulated into-table row count.
    html = _render_program_section(
        {
            "program": {
                "goal": "找 rating<=3 昵称",
                "statements": [
                    {"op": "run", "kind": "read", "var": "r", "name": "读候选行 id",
                     "returns": ["id"], "list_read": True},
                    {"op": "foreach", "var": "row", "over": "r", "into": "reviews", "body": [
                        {"op": "run", "kind": "action", "name": "打开评论 {row[id]} 详情"},
                        {"op": "run", "kind": "read", "var": "d", "name": "读评分昵称",
                         "returns": ["rating", "nickname"]},
                    ]},
                    {"op": "run", "kind": "data_query", "var": "q", "name": "筛 rating<=3",
                     "returns": ["nickname"], "sql": "SELECT ..."},
                    {"op": "finish", "message": "{q[nickname]}"},
                ],
            },
            "run_log": [
                {"var": "reviews", "result": {"rows": [{"id": "347"}, {"id": "349"}, {"id": "351"}]}},
            ],
            "context_reports": [],
        },
    )
    assert "foreach" in html
    assert "列表读取" in html          # the list_read badge
    assert "采集 3 行" in html          # accumulated into-table row count
    assert "打开评论" in html           # body Run rendered (not dropped)
    assert "prog-branch" in html        # body is indented under the loop


def test_thumb_time_only_renders_total_and_keeps_flags_searchable():
    html, search = _render_thumb_time(
        ReportStep(
            label="Turn 3",
            action_type="none",
            x=None,
            y=None,
            description="done check",
            annotated_before_url="",
            timings={"checker": 4.2},
            llm_context=[
                {"kind": "prompt_snapshot", "label": "checker"},
                {"kind": "prompt_snapshot", "label": "checker"},
            ],
            no_effect=True,
        )
    )

    assert "thumb-time" in html
    assert "4.2s" in html
    assert "checker 4.2s" not in html
    assert "no_effect" not in html
    assert "no_effect" in search
    assert "重复 checker x2" in search


def test_thumb_time_prefers_wall_clock_gap_when_timestamp_available():
    html, search = _render_thumb_time(
        ReportStep(
            label="Turn 4",
            action_type="tap",
            x=None,
            y=None,
            description="click link",
            annotated_before_url="",
            timestamp="2026-06-18T22:03:44",
            timings={"checker": 1.9, "planner": 1.8},
        ),
        prev_timestamp="2026-06-18T22:03:37",
    )

    assert "+7s" in html
    assert "3.7s" not in html
    assert "wall_gap 7.0s" in search


def test_first_turn_thumb_time_uses_elapsed_estimate_after_decompose():
    html, search = _render_thumb_time(
        ReportStep(
            label="Turn 1",
            action_type="tap",
            x=None,
            y=None,
            description="click menu",
            annotated_before_url="",
            timestamp="2026-06-18T22:03:37",
            timings={"checker": 1.2, "planner": 2.3, "action_policy": 1.0},
        )
    )

    assert "+4s" in html
    assert "从编排完成后起算" in html
    assert "first_turn_elapsed_estimate 4.5s" in search


def test_milestone_elapsed_matches_turn_badge_elapsed_sum():
    first = ReportStep(
        label="Turn 1",
        action_type="tap",
        x=None,
        y=None,
        description="click menu",
        annotated_before_url="",
        timestamp="2026-06-18T22:03:37",
        timings={"checker": 1.2, "selector": 0.9, "planner": 2.3, "action_policy": 1.0},
    )
    second = ReportStep(
        label="Turn 2",
        action_type="tap",
        x=None,
        y=None,
        description="click link",
        annotated_before_url="",
        timestamp="2026-06-18T22:03:44",
        timings={"checker": 1.9, "selector": 0.7, "planner": 1.8, "action_policy": 1.0},
    )

    first_elapsed, first_kind = _turn_elapsed_seconds(first)
    second_elapsed, second_kind = _turn_elapsed_seconds(second, first.timestamp)

    assert first_kind == "first_turn_elapsed_estimate"
    assert second_kind == "wall_gap"
    assert round(first_elapsed + second_elapsed, 1) == 12.4


# ── program-aligned milestone grouping (builder._group_steps_by_milestone) ───────
def _gstep(mid: str, n: int = 1) -> ReportStep:
    return ReportStep(
        label=f"Turn {n}", action_type="tap", x=1.0, y=1.0,
        description=mid, annotated_before_url="", milestone_id=mid,
    )


def test_group_merges_non_contiguous_revisit_into_one_card():
    # Real run shape: m1 turns 1-9, then d 10-12, q 13, then m1 AGAIN at 14.
    # The revisit must merge into ONE m1 card, not split into two.
    steps = (
        [_gstep("m1", i) for i in range(1, 10)]
        + [_gstep("d", 10), _gstep("d", 11), _gstep("d", 12)]
        + [_gstep("q", 13)]
        + [_gstep("m1", 14)]
    )
    prog = [{"id": "m0_navigation"}, {"id": "m1"}, {"id": "d"}, {"id": "q"}]
    pages = _group_steps_by_milestone(steps, prog, {})
    assert [p.milestone_id for p in pages] == ["m0_navigation", "m1", "d", "q"]
    m1 = next(p for p in pages if p.milestone_id == "m1")
    assert len(m1.steps) == 10  # turns 1-9 + 14 merged into one card
    assert [s.label for s in m1.steps] == [f"Turn {i}" for i in [*range(1, 10), 14]]


def test_group_keeps_zero_step_milestone_card():
    # A startup navigation that completes before the first interactive turn has 0 steps but
    # must still get a card so the execution view lines up 1:1 with the #0 program card.
    # (In production ms_lookup is built from ctx["milestones"], so it carries kind/name.)
    steps = [_gstep("m1", 1)]
    prog = [{"id": "m0_navigation"}, {"id": "m1"}]
    ms_lookup = {"m0_navigation": {"kind": "navigation", "name": "进入评论页"}, "m1": {"kind": "filter"}}
    pages = _group_steps_by_milestone(steps, prog, ms_lookup)
    assert [p.milestone_id for p in pages] == ["m0_navigation", "m1"]
    assert pages[0].steps == []                 # zero steps, but a card exists
    assert pages[0].milestone_kind == "navigation"  # kind/meta come from ms_lookup
    assert pages[0].milestone_name == "进入评论页"


def test_group_orphans_to_trailing_card_in_first_seen_order():
    # Steps whose milestone_id isn't a known program milestone → trailing uncategorized card,
    # in first-seen order, so no turn is silently dropped.
    steps = [_gstep("m1", 1), _gstep("non_ui_5", 2), _gstep("_no_milestone", 3)]
    prog = [{"id": "m1"}]
    pages = _group_steps_by_milestone(steps, prog, {})
    assert [p.milestone_id for p in pages] == ["m1", "non_ui_5", "_no_milestone"]


def test_group_uses_program_order_not_turn_order():
    # d executes before m1 in the stream, but the program lists m1 before d → cards follow program.
    steps = [_gstep("d", 1), _gstep("m1", 2)]
    prog = [{"id": "m1"}, {"id": "d"}]
    pages = _group_steps_by_milestone(steps, prog, {})
    assert [p.milestone_id for p in pages] == ["m1", "d"]
