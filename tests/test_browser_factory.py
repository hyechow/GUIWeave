from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from gui_agent.adapters.browser import factory, perception


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


def test_headless_probe_leaves_an_active_asyncio_thread(monkeypatch) -> None:
    caller_thread = threading.get_ident()
    probe_threads: list[int] = []
    monkeypatch.setattr(
        factory,
        "_launch_headless_chromium",
        lambda: probe_threads.append(threading.get_ident()) or "/plugin-cache/chromium",
    )

    async def probe() -> str:
        return factory._probe_headless_chromium()

    assert asyncio.run(probe()) == "/plugin-cache/chromium"
    assert probe_threads and probe_threads[0] != caller_thread


def test_browser_bundle_defaults_start_url_to_google(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    session = object()
    monkeypatch.setattr(
        perception,
        "BrowserSession",
        lambda **options: captured.append(options) or session,
    )

    assert factory.build_browser_bundle().open_session() is session
    assert captured[-1]["start_url"] == factory.DEFAULT_BROWSER_START_URL

    assert factory.build_browser_bundle(start_url="https://example.com/").open_session() is session
    assert captured[-1]["start_url"] == "https://example.com/"
