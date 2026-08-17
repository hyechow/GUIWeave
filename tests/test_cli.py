import inspect

from gui_agent.adapters.browser.factory import DEFAULT_BROWSER_START_URL
from gui_agent.cli import _build_parser, _platform_options
from gui_agent.mcp_server import run_browser_task


def test_browser_start_url_is_consistent_across_cli_and_mcp() -> None:
    parser = _build_parser()
    default = parser.parse_args(["run", "browser", "inspect"])
    custom = parser.parse_args([
        "run",
        "browser",
        "inspect",
        "--start-url",
        "https://example.com/start",
    ])

    assert _platform_options(default)["start_url"] == DEFAULT_BROWSER_START_URL
    assert _platform_options(custom)["start_url"] == "https://example.com/start"
    args = _build_parser().parse_args(["check", "browser", "--headless"])
    assert _platform_options(args) == {"cdp_url": None, "headless": True}
    parameter = inspect.signature(run_browser_task).parameters["start_url"]
    assert parameter.default == DEFAULT_BROWSER_START_URL
