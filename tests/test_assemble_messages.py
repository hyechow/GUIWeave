"""assemble_messages: the single context entry path for milestone vision calls.

Locks two things: (1) kept blocks land exactly where the legacy _build_msgs + _inject_* path
put them (system-prompt tail / human-message head), and (2) budgeting is ONE pass over the
system+human UNION (a per-prompt ceiling), not the old per-call-site cap.
"""

from __future__ import annotations

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import acceptance_items_block, form_controls_block
from gui_agent.core.llm.messages import assemble_messages


def _texts(human_content: list) -> list[str]:
    return [p["text"] for p in human_content if p.get("type") == "text"]


def test_placement_matches_legacy_layout():
    msgs = assemble_messages(
        "TASK_PROMPT",
        b"x",
        system_blocks=[acceptance_items_block(["子项A"])],
        human_blocks=[form_controls_block([{"kind": "native_select", "label": "Status",
                                            "options": ["Open", "Closed"]}])],
        image_resize="none",
        label="checker",
    )
    system = msgs[0].content
    # system prompt tail: TASK_PROMPT, then the system block body, then the date line (order
    # preserved). Blocks render headerless for the model (no [context:...] machine tag), so we
    # assert on the block's own markdown header.
    assert system.startswith("TASK_PROMPT")
    assert "[context:" not in system            # provenance tags are report-only, not in the prompt
    assert system.index("TASK_PROMPT") < system.index("逐项验收") < system.index("当前日期")

    human = msgs[1].content
    # human head: the form-controls block first, then the fixed decision text, then the image
    assert "浏览器 DOM 表单控件" in human[0]["text"]
    assert _texts(human)[-1] == "请根据当前屏幕做出决策。"
    assert human[-1]["type"] == "image_url"


def test_empty_blocks_produce_bare_message():
    msgs = assemble_messages("TASK", b"x", image_resize="none")
    # no blocks → system is just task + date, human is just decision + image (no leading text)
    assert msgs[0].content == "TASK\n\n" + msgs[0].content.split("\n\n", 1)[1]
    human = msgs[1].content
    assert human[0]["text"] == "请根据当前屏幕做出决策。"
    assert human[1]["type"] == "image_url"


def test_budget_is_one_pass_over_system_human_union():
    # A system block + a human block, each under the cap alone but together over it. The old
    # per-call-site budgeting kept both (each got the full cap); the union pass must drop one.
    sys_block = ContextBlock("sys", "runtime_state", "t", "S" * 500, budget="required")
    hum_block = ContextBlock("hum", "runtime_state", "t", "H" * 500, budget="low")
    msgs = assemble_messages(
        "T", b"x",
        system_blocks=[sys_block],
        human_blocks=[hum_block],
        image_resize="none",
        max_chars=600,   # < the ~1100-char union, > either block alone
    )
    system = msgs[0].content
    human_text = "".join(_texts(msgs[1].content))
    assert "SSSS" in system          # required block kept
    assert "HHHH" not in human_text  # low-tier human block dropped by the UNION budget pass
    assert _texts(msgs[1].content)[0] == "请根据当前屏幕做出决策。"  # no leading block text


def test_context_reports_include_prompt_snapshot_segments():
    reports: list[dict] = []
    sys_block = ContextBlock("sys", "runtime_state", "unit", "SYS", budget="required")
    hum_block = ContextBlock("hum", "runtime_state", "unit", "HUM", budget="high")

    assemble_messages(
        "TASK",
        b"x",
        system_blocks=[sys_block],
        human_blocks=[hum_block],
        image_resize="none",
        label="unit.call",
        context_reports=reports,
    )

    snapshot = next(r for r in reports if r["kind"] == "prompt_snapshot")
    assert snapshot["label"] == "unit.call"
    roles = {role["role"]: role["parts"] for role in snapshot["roles"]}
    assert [part["label"] for part in roles["system"]][:2] == ["task_prompt", "sys"]
    assert [part["label"] for part in roles["human"]][:3] == ["hum", "decision_text", "screenshot"]
    assert roles["system"][0]["text"] == "TASK"
    assert roles["human"][0]["text"] == "HUM"
    assert "image_url omitted" in roles["human"][-1]["text"]


def test_report_module_io_combines_input_and_output():
    from gui_agent.reports.prompt_html import _render_module_io_html

    html = _render_module_io_html([
        {
            "kind": "prompt_snapshot",
            "label": "checker",
            "roles": [
                {"role": "system", "parts": [
                    {"label": "task_prompt", "text": "TASK", "chars": 4},
                    {"label": "schema_instruction", "text": "SCHEMA", "chars": 6},
                ]},
                {"role": "human", "parts": [
                    {"label": "decision_text", "text": "GO", "chars": 2},
                ]},
            ],
        },
        {
            "kind": "llm_output",
            "label": "checker",
            "schema": "Check",
            "mode": "json_object",
            "raw_output": '{"status":"done"}',
            "parsed": {"status": "done"},
            "chars": 17,
        },
    ])

    assert "模型调用详情" in html
    assert "schema_instruction" in html
    assert "raw_output" in html
    assert "parsed" in html
    assert "status" in html
