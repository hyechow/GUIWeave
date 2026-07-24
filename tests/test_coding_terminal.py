from gui_agent.core.coding_orchestrator.models import CodingEvent
from gui_agent.core.coding_orchestrator.terminal import CodingTerminalRenderer


def test_terminal_renderer_shows_source_review_and_repair_diff() -> None:
    lines = []
    render = CodingTerminalRenderer(write=lines.append)
    initial = "def run(ctx):\n    return 1 / 0"
    repaired = "def run(ctx):\n    return 1"

    for event in [
        CodingEvent("generation_started", {"goal": "return one"}),
        CodingEvent("generation_completed", {"source": initial, "seconds": 1.25}),
        CodingEvent("diagnostics", {
            "phase": "initial",
            "status": "passed",
            "diagnostics": [],
        }),
        CodingEvent("probe", {
            "phase": "initial",
            "status": "failed",
            "operations": [],
            "error": "ZeroDivisionError: division by zero",
        }),
        CodingEvent("review_started", {"pass_index": 1}),
        CodingEvent("review_completed", {
            "pass_index": 1,
            "approved": False,
            "source": initial,
            "edits": [{"search": "1 / 0", "replacement": "1"}],
            "text": '{"approve": false}',
            "error": "",
            "seconds": 0.5,
        }),
        CodingEvent("repair_completed", {
            "status": "accepted",
            "before": initial,
            "after": repaired,
            "proposed": repaired,
            "selected_edits": [0],
            "error": "",
            "candidate_diagnostics": [],
            "candidate_error": "",
        }),
        CodingEvent("finalized", {
            "status": "passed",
            "source": repaired,
            "repair_status": "accepted",
        }),
    ]:
        render(event)

    output = "\n".join(lines)
    assert "[coding] Generate" in output
    assert "   2 │     return 1 / 0" in output
    assert "ZeroDivisionError: division by zero" in output
    assert "Changes requested · 1 edit(s)" in output
    assert "candidate.py (review)" in output
    assert "raw:" not in output
    assert "-    return 1 / 0" in output
    assert "+    return 1" in output
    assert "[coding] Final" in output


def test_terminal_renderer_shows_rejected_repair_and_reason() -> None:
    lines = []
    render = CodingTerminalRenderer(write=lines.append)
    initial = "def run(ctx):\n    ctx.interact('Save')"
    proposed = (
        "def run(ctx):\n"
        "    assert ctx.interact('Save', success='saved')"
    )

    render(CodingEvent("repair_completed", {
        "status": "rejected",
        "before": initial,
        "after": initial,
        "proposed": proposed,
        "selected_edits": [0],
        "error": "",
        "candidate_diagnostics": [
            "[BUSINESS_ASSERTION_MESSAGE] line 2: "
            "business assertions require a failure message"
        ],
        "candidate_error": "",
    }))

    output = "\n".join(lines)
    assert "✗ rejected" in output
    assert "+    assert ctx.interact('Save', success='saved')" in output
    assert "BUSINESS_ASSERTION_MESSAGE" in output
