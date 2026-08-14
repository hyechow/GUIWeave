from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.browser import factory


def test_headless_preflight_launches_playwright_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        factory,
        "_probe_headless_chromium",
        lambda: "/plugin-cache/chromium",
    )

    result = factory._setup_check(None, headless=True)

    assert result.ok
    assert result.summary == "Playwright Chromium 已就绪"
    assert any("/plugin-cache/chromium" in line for line in result.lines)
    assert any("无需外部 Chrome CDP" in line for line in result.lines)
    session = SimpleNamespace(client=SimpleNamespace(headless=True))
    assert factory._make_action_visualizer(session) is None


def test_headless_preflight_reports_install_command(monkeypatch) -> None:
    def fail() -> str:
        raise RuntimeError("browser executable missing")

    monkeypatch.setattr(factory, "_probe_headless_chromium", fail)

    result = factory._setup_check(None, headless=True)

    assert not result.ok
    assert result.summary == "Playwright Chromium 不可用"
    assert any("playwright install chromium" in line for line in result.lines)
