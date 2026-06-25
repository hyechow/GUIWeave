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


def test_orchestrator_program_renders_dataflow_lane():
    html = _render_program_section(
        {
            "program": {
                "goal": "count reviews",
                "statements": [
                    {
                        "op": "run",
                        "kind": "read",
                        "var": "r",
                        "name": "read count",
                        "returns": ["total_count"],
                    },
                    {"op": "finish", "message": "{r[total_count]}"},
                ],
            },
            "run_log": [
                {
                    "var": "r",
                    "result": {"reads": {"total_count": "6"}},
                }
            ],
            "context_reports": [],
        },
        {
            "agent_response": {"retrieved_data": [6]},
            "eval_result": {
                "evaluators_results": [
                    {
                        "actual_normalized": {"retrieved_data": [6.0]},
                        "expected": {"retrieved_data": [6.0]},
                    }
                ]
            },
        },
    )

    assert "Dataflow" in html
    assert "read r.total_count=6" in html
    assert "WebArena Response [6.0]" in html
    assert "Answer [6.0]" in html
    assert "旧日志缺 prompt snapshot" in html


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
        None,
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
