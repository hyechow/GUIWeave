from gui_agent.core.orchestrator.models import CodingEvent
from gui_agent.core.orchestrator.terminal import CodingTerminalRenderer


def test_terminal_renderer_shows_review_issues_and_regeneration() -> None:
    lines = []
    render = CodingTerminalRenderer(write=lines.append)

    for event in [
        CodingEvent("generation_started", {"goal": "return one", "phase": "initial"}),
        CodingEvent("generation_completed", {
            "phase": "initial",
            "source": "def run(ctx):\n    return 1 / 0",
            "seconds": 1.25,
        }),
        CodingEvent("review_started", {"pass_index": 1}),
        CodingEvent("review_completed", {
            "pass_index": 1,
            "approved": False,
            "issues": ["[ZERO_DIVISION] source: result cannot be computed"],
            "error": "",
            "seconds": 0.5,
        }),
        CodingEvent("generation_started", {
            "goal": "return one",
            "phase": "regenerated",
        }),
        CodingEvent("finalized", {
            "status": "passed",
            "source": "def run(ctx):\n    return 1",
            "repair_status": "completed",
        }),
    ]:
        render(event)

    output = "\n".join(lines)
    assert "Generate · initial" in output
    assert "Rejected · 1 issue(s)" in output
    assert "ZERO_DIVISION" in output
    assert "Generate · regenerated" in output
    assert "regeneration completed" in output
