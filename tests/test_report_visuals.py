import json

from gui_agent.reports.builder import RunnerReportBuilder
from gui_agent.reports.builder import _group_steps_by_statement
from gui_agent.reports.models import ReportStep
from gui_agent.reports.orchestrator_html import (
    _render_non_ui_detail,
    _render_program_section,
)
from gui_agent.reports.prompt_html import _render_module_io_html
from gui_agent.reports.runner_html import generate_html, _render_thumb_time, _turn_elapsed_seconds


def test_module_io_renders_summary_collapsed_schema_and_tokens():
    html = _render_module_io_html([
        {
            "kind": "prompt_snapshot",
            "label": "transition",
            "roles": [
                {"role": "system", "parts": [
                    {
                        "label": "task_prompt",
                        "text": "TASK",
                        "chars": 4,
                    },
                    {
                        "label": "schema_instruction",
                        "text": "顶层必填字段：kind, reason\n顶层可选字段：summary",
                        "chars": 32,
                    },
                ]},
            ],
        },
        {
            "kind": "llm_output",
            "label": "transition",
            "schema": "_StatementTransitionResult",
            "raw_output": '{"kind":"complete","reason":"ok"}',
            "parsed": {"kind": "complete", "reason": "ok"},
        },
    ], {"transition": {"input": 4108, "output": 73}})

    assert "prompt-call-summary" in html
    assert "complete · ok" in html
    assert "prompt-detail-meta" in html
    assert "1 call · 4.1k/73 tok" in html
    assert "prompt-token-detail" in html
    assert "Token 明细" in html
    assert "prompt-schema" in html
    assert "prompt-part-collapsed" in html
    assert "schema_instruction · _StatementTransitionResult · 2 required / 1 optional" in html
    assert "prompt-token-total" not in html
    assert "transition" in html


def test_orchestrator_program_renders_foreach_block_and_body():
    html = _render_program_section(
        {
            "program": {
                "goal": "找 rating<=3 昵称",
                "statements": [
                    {
                        "op": "data",
                        "id": "s1",
                        "bind": "selection",
                        "goal": "选择 rating<=3 的评论",
                        "returns": {"rows": {"type": "list[record]"}},
                    },
                    {
                        "op": "foreach",
                        "items": {"var": "selection", "path": ["rows"]},
                        "item": "row",
                        "into": "reviews",
                        "collect": {"var": "detail", "path": ["nickname"]},
                        "body": [
                            {
                                "op": "interact",
                                "id": "s2",
                                "bind": "detail",
                                "goal": "打开当前评论并读取评分昵称",
                                "success": "当前评论详情可见",
                                "returns": {"nickname": {"type": "text"}},
                            }
                        ],
                    },
                    {"op": "finish", "message": "done"},
                ],
            },
            "report_run_log": [
                {
                    "var": "selection",
                    "result": {"outputs": {"rows": [{"id": "347"}, {"id": "349"}]}},
                },
            ],
            "context_reports": [],
        },
    )
    assert "foreach" in html
    assert "打开当前评论" in html
    assert "prog-branch" in html


def test_acquire_outputs_are_bounded_in_program_and_non_ui_details():
    rows = [
        {"id": f"row-{index}", "email": f"user-{index}@example.test"}
        for index in range(12)
    ]
    program_html = _render_program_section({
        "program": {
            "goal": "collect every row",
            "statements": [{
                "op": "acquire",
                "id": "s1",
                "bind": "collection",
                "goal": "collect rows",
                "returns": {"rows": {"type": "list[record]"}},
            }],
        },
        "report_run_log": [{
            "var": "collection",
            "result": {"outputs": {"rows": rows}},
        }],
        "context_reports": [],
    })
    detail_html = _render_non_ui_detail({
        "executor": "acquire",
        "outputs": {"rows": rows},
    })

    assert "rows=12 records · 2 fields [id, email]" in program_html
    assert "row-0" not in program_html
    assert "12 records · 2 fields [id, email]" in detail_html
    assert "sample (first 2)" in detail_html
    assert "row-0" in detail_html
    assert "row-1" in detail_html
    assert "row-2" not in detail_html


def test_in_progress_program_card_does_not_require_report_run_log():
    html = _render_program_section({
        "program": {
            "goal": "open one row",
            "statements": [
                {
                    "op": "data",
                    "id": "s1",
                    "bind": "row",
                    "goal": "read row",
                    "returns": {"id": {"type": "text"}},
                },
                {
                    "op": "interact",
                    "id": "s2",
                    "goal": "open selected row",
                    "success": "selected row is open",
                    "inputs": {"id": {"var": "row", "path": ["id"]}},
                },
            ],
        },
        "context_reports": [],
    })

    assert "read row" in html
    assert "open selected row" in html
    assert "prog-resolved" not in html


def test_orchestrator_program_renders_value_ref_path_in_if_condition():
    html = _render_program_section({
        "program": {
            "goal": "inspect then collect",
            "statements": [
                {
                    "op": "if",
                    "cond": {
                        "ref": {"var": "source", "path": ["available"]},
                        "cmp": "==",
                        "value": False,
                    },
                    "then": [],
                    "otherwise": [],
                },
            ],
        },
        "context_reports": [],
    })

    assert 'source["available"]' in html


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


def test_statement_elapsed_matches_turn_badge_elapsed_sum():
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


# ── program-aligned statement grouping (builder._group_steps_by_statement) ───────
def _gstep(mid: str, iid: str, n: int = 1) -> ReportStep:
    return ReportStep(
        label=f"Turn {n}", action_type="tap", x=1.0, y=1.0,
        description=mid, annotated_before_url="", statement_id=mid, instance_id=iid,
    )


def test_group_merges_non_contiguous_revisit_into_one_card():
    steps = [_gstep("s1", "i1:s1", 1), _gstep("s2", "i2:s2", 2), _gstep("s1", "i1:s1", 3)]
    invocations = [
        {"id": "s1", "instance_id": "i1:s1"},
        {"id": "s2", "instance_id": "i2:s2"},
    ]
    lookup = {item["instance_id"]: item for item in invocations}

    pages = _group_steps_by_statement(steps, invocations, lookup)

    assert [page.instance_id for page in pages] == ["i1:s1", "i2:s2"]
    assert [step.label for step in pages[0].steps] == ["Turn 1", "Turn 3"]


def test_group_keeps_statement_and_invocation_ids_separate():
    steps = [_gstep("s1", "i1:s1"), _gstep("s1", "i2:s1", 2)]
    invocations = [
        {"id": "s1", "instance_id": "i1:s1"},
        {"id": "s1", "instance_id": "i2:s1"},
    ]
    lookup = {item["instance_id"]: item for item in invocations}

    pages = _group_steps_by_statement(steps, invocations, lookup)

    assert [page.statement_id for page in pages] == ["s1", "s1"]
    assert [page.instance_id for page in pages] == ["i1:s1", "i2:s1"]


def test_terminal_event_attaches_to_statement_without_empty_report_turn(tmp_path):
    (tmp_path / "context.json").write_text(
        json.dumps({
            "goal": "save then continue",
            "supervisor_policy_name": "statement",
            "action_policy_name": "browser",
            "journal": {
                "schema_version": 4,
                "events": [
                    {
                        "event_type": "statement_outcome",
                        "after_turn": 0,
                        "observation_source": "browser",
                        "observation_url": "screenshot_turn_1.png",
                        "statement_instance_id": "i1:s1",
                        "statement_id": "s1",
                        "statement": {
                            "id": "s1",
                            "executor": "interact",
                            "goal": "save",
                            "success": "saved",
                        },
                        "outcome": {
                            "phase": "completed",
                            "summary": "saved",
                            "verification": "confirmed",
                        },
                    },
                    {
                        "event_type": "turn",
                        "index": 1,
                        "operation_mode": "interactive",
                        "observation_source": "browser",
                        "observation_url": "screenshot_turn_1.png",
                        "statement_instance_id": "i2:s2",
                        "statement": {
                            "id": "s2",
                            "executor": "interact",
                            "goal": "continue",
                            "success": "next page open",
                        },
                        "supervisor": {
                            "action_intent": {
                                "instruction": "open next page",
                            },
                            "summary": "continue",
                            "statement_id": "s2",
                        },
                        "action_decision": {
                            "action": {
                                "action_type": "tap",
                                "description": "open next page",
                            },
                        },
                        "executed": True,
                    },
                ],
            },
        }),
        encoding="utf-8",
    )

    data = RunnerReportBuilder().build(tmp_path)

    assert data.stats["turns"] == 1
    assert [page.instance_id for page in data.pages] == ["i1:s1", "i2:s2"]
    assert data.pages[0].steps == []
    assert data.pages[0].verify_url == "screenshot_turn_1.png"
    assert [step.label for step in data.pages[1].steps] == ["Turn 1"]


def test_non_interactive_turn_renders_its_observation_frame(tmp_path):
    (tmp_path / "screenshot_read_0.png").write_bytes(b"png")
    (tmp_path / "context.json").write_text(
        json.dumps({
            "goal": "read current data",
            "journal": {
                "schema_version": 4,
                "events": [{
                    "event_type": "turn",
                    "index": 1,
                    "operation_mode": "non_interactive",
                    "observation_source": "browser",
                    "observation_url": "screenshot_read_0.png",
                    "statement_instance_id": "i1:data",
                    "statement": {
                        "id": "data",
                        "executor": "data",
                        "goal": "read current data",
                        "success": "data returned",
                    },
                    "supervisor": {"summary": "data returned", "statement_id": "data"},
                    "non_ui": {
                        "executor": "data",
                        "goal": "read current data",
                        "summary": "data returned",
                        "observation_url": "screenshot_read_0.png",
                    },
                    "executed": True,
                }],
            },
        }),
        encoding="utf-8",
    )

    data = RunnerReportBuilder().build(tmp_path)
    html = generate_html(data)

    assert data.pages[0].steps[0].raw_screenshot_url == "screenshot_read_0.png"
    assert data.pages[0].steps[0].status == "✓ Data"
    assert '<img src="screenshot_read_0.png"' in html
    assert "Data 数据处理" in html
    assert "Data 处理" in html
