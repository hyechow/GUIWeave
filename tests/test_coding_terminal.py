from gui_agent.core.orchestrator.models import CodingEvent
from gui_agent.core.orchestrator.terminal import CodingTerminalRenderer


def test_terminal_renderer_shows_diagnostics_and_regeneration() -> None:
    lines = []
    render = CodingTerminalRenderer(write=lines.append)

    for event in [
        CodingEvent("generation_started", {"goal": "return one", "phase": "initial"}),
        CodingEvent("generation_completed", {
            "phase": "initial",
            "source": "def run(ctx):\n    return 1 / 0",
            "seconds": 1.25,
        }),
        CodingEvent("diagnostics", {
            "phase": "initial",
            "status": "failed",
            "diagnostics": ["[ZERO_DIVISION] source: result cannot be computed"],
        }),
        CodingEvent("probe", {
            "phase": "initial",
            "status": "skipped",
            "operations": [],
            "return_value": "",
            "error": "",
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
    assert "Static Review · initial" in output
    assert "ZERO_DIVISION" in output
    assert "skipped because static diagnostics remain" in output
    assert "Generate · regenerated" in output
    assert "regeneration completed" in output
