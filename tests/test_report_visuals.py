import json

from gui_agent.reports.builder import RunnerReportBuilder
from gui_agent.reports.builder import _group_steps_by_statement
from gui_agent.reports.models import ReportData, ReportStep
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
                        "op": "read",
                        "id": "s1",
                        "bind": "selection",
                        "reads": {"rows": {"source": "dataset", "name": "rows"}},
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


def test_coding_orchestrator_renders_reviewed_python_plan():
    html = _render_program_section({
        "program": {
            "kind": "coding",
            "goal": "return the top record",
            "source": 'def run(ctx):\n    rows = ctx.acquire("items")\n    return rows[0]',
        },
        "context_reports": [{
            "kind": "coding_review",
            "approved": False,
            "repaired": True,
        }],
        "timings": {"orchestrator.coding": 1.5},
    })

    assert "Coding Orchestrator · Python 执行计划" in html
    assert "Review · 已修复" in html
    assert "ctx.acquire" in html
    assert "coding-source-wrap" in html
    assert "完整 Python 源码" in html
    assert "coding-trace" not in html  # nodes attached by generate_html, not the shell


def test_coding_report_uses_standard_cards_with_data_strip():
    """Coding mode keeps standard statement cards; each card gains a data strip above gallery."""
    from gui_agent.reports.models import ReportPage, ReportStep

    html = generate_html(ReportData(
        title="coding report",
        orchestrator={
            "program": {
                "kind": "coding",
                "goal": "open orders",
                "source": (
                    'def run(ctx):\n    state = ctx.gui("Open orders", '
                    'success={"entity": "Orders", '
                    '"fields": ["Status", "Purchase Date"]})\n'
                    '    return ctx.query(state, entity="Orders", '
                    'fields=["Status", "Purchase Date"], '
                    'filters={"Status": "Complete"})\n'
                ),
            },
            "report_run_log": [{
                "node_id": "c1",
                "executor": "interact",
                "name": "go_to",
                "instance_id": "i1:c1",
                "coding_op": "gui",
                "coding_payload": {
                    "goal": "Open orders",
                    "success": {
                        "entity": "Orders",
                        "fields": ["Status", "Purchase Date"],
                    },
                    "produced_state": "ui:1",
                },
                "result": {
                    "phase": "completed",
                    "summary": "arrived at Orders",
                    "outputs": {},
                    "evidence": ["url contains /order/"],
                },
            }, {
                "node_id": "c2",
                "executor": "interact",
                "name": "Resolve collection Orders",
                "instance_id": "i2:c2",
                "coding_op": "lookup",
                "coding_payload": {
                    "state": "ui:1",
                    "entity": "Orders",
                    "filters": {"Status": "Complete"},
                    "required_fields": ["Status", "Purchase Date"],
                },
                "result": {
                    "phase": "exhausted",
                    "summary": "lookup failed",
                    "failure_evidence": "query-only lookup cannot commit",
                    "outputs": {},
                },
            }],
            "context_reports": [{
                "kind": "coding_review",
                "approved": True,
                "repaired": False,
            }],
        },
        statements=[
            {
                "id": "c1", "instance_id": "i1:c1", "name": "go_to",
                "executor": "interact",
                "inputs": {"target": "Orders List"},
                "call": {
                    "id": "c1",
                    "executor": "interact",
                    "goal": "go_to",
                    "success": "The GUI task is complete: go_to",
                    "persistence": "immediate",
                    "inputs": {"target": "Orders List"},
                    "required_values": {},
                    "observe_fields": [],
                },
            },
            {
                "id": "c2", "instance_id": "i2:c2",
                "name": "Resolve collection Orders", "executor": "interact",
                "inputs": {"lookup_request": {"entity": "Orders"}},
                "call": {
                    "id": "c2",
                    "executor": "interact",
                    "goal": "Resolve collection Orders",
                    "persistence": "immediate",
                    "inputs": {
                        "lookup_request": {
                            "entity": "Orders",
                            "field": "name",
                            "fallback": "",
                            "filters": {"Status": "Complete"},
                            "required_fields": ["Status", "Purchase Date"],
                        },
                    },
                },
            },
        ],
        pages=[
            ReportPage(
                title="go_to",
                statement_id="c1",
                instance_id="i1:c1",
                statement_executor="interact",
                statement_name="go_to",
                statement_success="The GUI task is complete: go_to",
                steps=[ReportStep(
                    label="Turn 1",
                    action_type="tap",
                    x=10,
                    y=20,
                    description="click sales",
                    annotated_before_url="shot1.jpg",
                    status="✓",
                    statement_id="c1",
                    instance_id="i1:c1",
                )],
            ),
            ReportPage(
                title="lookup",
                statement_id="c2",
                instance_id="i2:c2",
                statement_executor="interact",
                statement_name="Resolve collection Orders",
                statement_success="collection resolved",
                steps=[],
            ),
        ],
    ))

    # Same card shell as DSL reports — not a custom details accordion.
    assert "coding-trace" not in html
    assert 'id="ms-c1"' in html
    assert 'id="ms-c2"' in html
    # Runtime calls annotated on Python source lines (not a separate index block).
    assert "coding-call-index" not in html
    assert "coding-source-annotated" in html
    assert "coding-src-chip" in html
    assert 'href="#ms-c1"' in html
    assert "coding-stmt-data" in html
    assert "ctx.gui" in html
    assert "success=collection" in html
    assert "ui:1" in html
    assert "Orders List" in html or "Orders" in html
    assert "Status" in html  # filters / required_fields
    assert "arrived at Orders" in html
    assert "query-only lookup cannot commit" in html
    assert 'class="gallery"' in html
    assert "shot1.jpg" in html
    assert "coding-phase-ok" in html
    assert "coding-phase-fail" in html
    assert "coding-ctx-badge" in html
    # Header subtitle shows the underlying call (not DSL 验收 criteria).
    c1 = html.split('id="ms-c1"', 1)[1].split('id="ms-c2"', 1)[0]
    assert "coding-call-sc" in c1
    assert "验收：" not in c1
    assert "ctx.gui" in c1
    assert "success=collection" in c1
    assert "ui:1" in c1
    assert "Orders List" in c1 or "Orders" in c1
    # Data strip sits inside the standard card before gallery and includes full call params.
    assert "coding-stmt-data" in c1
    assert c1.find("coding-stmt-data") < c1.find('class="gallery"')
    assert "调用参数" in c1 or "本步参数" in c1
    assert "Orders List" in c1
    assert "Statement 执行器契约" in c1  # full contract folded
    assert "运行结果" in c1
    assert "<summary>调用参数</summary>" in c1
    assert "<summary>运行结果</summary>" in c1
    assert '<details class="coding-data-details" open' not in c1
    # Light unified surface — no dark code dump as primary view.
    assert "coding-data-pre-full" not in html
    # query macro expansion: top verdict + folded plan details + step args.
    c2 = html.split('id="ms-c2"', 1)[1].split('class="gallery"', 1)[0] if 'class="gallery"' in html.split('id="ms-c2"', 1)[1] else html.split('id="ms-c2"', 1)[1][:5000]
    assert "coding-macro-verdict" in c2
    assert "步骤 1/3" in c2 or "1/3" in c2
    assert "lookup" in c2
    assert "constrain" in c2  # pending / 未执行
    assert "acquire" in c2
    assert "本步参数" in c2
    assert "<summary>本步参数</summary>" in c2
    assert 'coding-plan-details" open' not in c2
    # Header: one combined badge, call signature only (no duplicate plan subtitle line).
    assert "ctx.query 1/3" in c2 or "ctx.query 1/3 · lookup" in c2
    assert c2.count("coding-plan-sc") == 0


def test_runner_report_omits_subgoal_outline_sidebar():
    html = generate_html(ReportData(
        title="report",
        statements=[{
            "id": "step_1",
            "name": "do work",
            "kind": "interact",
            "total_time": 1.0,
        }],
    ))

    assert "子目标分解" not in html
    assert 'class="outline' not in html
    assert '<nav class="sidebar">' not in html


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
                    "op": "read",
                    "id": "s1",
                    "bind": "row",
                    "reads": {"id": {"source": "field", "name": "row id"}},
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

    assert "row id" in html
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


def test_orchestrator_program_collapses_compiler_acquire_macro():
    html = _render_program_section({
        "program": {
            "goal": "count records by month",
            "statements": [
                {
                    "op": "source_check", "bind": "__source_initial",
                    "required_fields": ["date", "status"],
                },
                {
                    "op": "if",
                    "cond": {
                        "ref": {"var": "__source_initial", "path": ["available"]},
                        "cmp": "==", "value": False,
                    },
                    "then": [{"op": "interact", "goal": "expose required fields"}],
                    "otherwise": [],
                },
                {
                    "op": "source_check", "bind": "__source_final",
                    "required_fields": ["date", "status"],
                },
                {
                    "op": "acquire", "bind": "__collection",
                    "goal": "collect the scoped records",
                    "source_check": {"var": "__source_final", "path": ["available"]},
                    "required_fields": ["date", "status"],
                    "returns": {"rows": {"type": "list[record]", "coverage": "complete"}},
                },
                {
                    "op": "finish", "message": "",
                    "outputs": {"result": {"var": "answer", "path": ["result"]}},
                },
            ],
        },
        "report_run_log": [
            {"var": "__source_initial", "result": {"outputs": {"available": True}}},
            {"var": "__source_final", "result": {"outputs": {"available": True}}},
            {"var": "__collection", "result": {"outputs": {"rows": [{"id": "1"}]}}},
        ],
        "context_reports": [],
    })

    assert "prog-compiler-detail" in html
    assert "采集完整集合" in html
    assert "Compiler 自动步骤" in html
    assert "初检</b> · 可读" in html
    assert "修复</b> · 已跳过" in html
    assert "__source_initial" not in html
    assert "完成并返回 result" in html
    assert "finish「」" not in html


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
                    "statement_instance_id": "i1:read",
                    "statement": {
                        "id": "read",
                        "executor": "read",
                        "goal": "read current data",
                        "success": "data returned",
                    },
                    "supervisor": {"summary": "data returned", "statement_id": "read"},
                    "non_ui": {
                        "executor": "read",
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
    assert data.pages[0].steps[0].status == "✓ Read"
    assert '<img src="screenshot_read_0.png"' in html
    assert "Read 观察绑定" in html
