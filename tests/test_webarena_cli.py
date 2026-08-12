from __future__ import annotations

from gui_agent.adapters.browser.webarena import _build_parser


def test_webarena_enables_tool_agent_multi_action_by_default() -> None:
    parser = _build_parser()
    required = [
        "--tasks-file",
        "tasks.json",
        "--task-id",
        "1",
        "--task-output-dir",
        "output/1",
    ]

    enabled = parser.parse_args(required)
    disabled = parser.parse_args([*required, "--no-multi-action"])

    assert enabled.multi_action is True
    assert disabled.multi_action is False
