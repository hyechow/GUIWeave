from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.iphone.actions import IPhoneAction
from gui_agent.adapters.iphone.client import MirrorDaemonClient
from gui_agent.adapters.iphone.executor import IPhoneExecutor
from gui_agent.adapters.iphone.perception import IPhoneSession
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.runtime.platforms import PLATFORMS
from gui_agent.core.tool_agent.protocol import worker_action_floor


def test_iphone_bundle_matches_shared_platform_contract() -> None:
    assert PLATFORMS == ("browser", "android", "iphone")
    bundle = build_platform("iphone")

    assert bundle.platform == "iphone"
    assert set(bundle.tool_agent_capabilities) == {
        "tap", "type", "clear_text", "press_enter", "scroll", "drag",
        "home", "app_switch",
    }
    assert isinstance(bundle.make_action({
        "action_type": "home",
        "description": "return home",
    }), IPhoneAction)
    floor = worker_action_floor(bundle.tool_agent_capabilities)
    assert {action.capability for action in floor} == set(
        bundle.tool_agent_capabilities
    )


def test_all_registered_platforms_expose_the_same_bundle_contract() -> None:
    callable_fields = (
        "open_session",
        "setup_check",
        "make_executor",
        "make_action",
        "make_perception",
        "make_status_reporter",
        "make_action_visualizer",
    )

    for platform in PLATFORMS:
        bundle = build_platform(platform)
        assert bundle.platform == platform
        assert bundle.tool_agent_capabilities
        assert all(callable(getattr(bundle, name)) for name in callable_fields)


def test_iphone_backend_roles_cannot_be_overridden() -> None:
    try:
        build_platform("iphone", backend="daemon")
    except ValueError as exc:
        assert "sck_server screenshots and mirror_daemon input" in str(exc)
    else:  # pragma: no cover - protects the fixed helper boundary
        raise AssertionError("iPhone backend override should be rejected")


def test_iphone_session_always_uses_sck_for_screenshots() -> None:
    calls: list[str] = []
    session = IPhoneSession()
    session._sck = SimpleNamespace(screenshot=lambda: calls.append("sck") or b"png")
    session.client = SimpleNamespace(screenshot=lambda: calls.append("daemon") or b"bad")

    assert session.screenshot() == b"png"
    assert calls == ["sck"]
    assert not hasattr(MirrorDaemonClient, "screenshot")


def test_iphone_session_starts_sck_and_daemon_with_fixed_roles(monkeypatch) -> None:
    events: list[str] = []

    class FakeSCK:
        def start(self) -> None:
            events.append("sck:start")

        def screenshot(self) -> bytes:
            events.append("sck:screenshot")
            return b"frame"

        def close(self) -> None:
            events.append("sck:close")

    class FakeDaemon:
        def connect(self) -> None:
            events.append("daemon:connect")

        def close(self) -> None:
            events.append("daemon:close")

    monkeypatch.setattr(
        "gui_agent.adapters.iphone.perception.SCKScreenshotClient", FakeSCK
    )
    monkeypatch.setattr(
        "gui_agent.adapters.iphone.perception.MirrorDaemonClient", FakeDaemon
    )

    with IPhoneSession() as session:
        assert session.screenshot() == b"frame"

    assert events == [
        "sck:start",
        "daemon:connect",
        "sck:screenshot",
        "sck:close",
        "daemon:close",
    ]


class _FakeClient:
    viewport_size = (318, 701)

    def press_home(self) -> str:
        return "OK home"

    def app_switch(self) -> str:
        return "OK appswitch"


def test_iphone_executor_dispatches_platform_actions() -> None:
    executor = IPhoneExecutor(SimpleNamespace(client=_FakeClient()))
    home = SimpleNamespace(action=IPhoneAction(
        action_type="home", description="home"
    ))

    assert executor.execute(home) is True
