from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.iphone.actions import IPhoneAction
from gui_agent.adapters.iphone.client import MirrorDaemonClient, _helper_path
from gui_agent.adapters.iphone import factory as iphone_factory
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


def test_iphone_helpers_accept_plugin_asset_overrides(
    tmp_path,
    monkeypatch,
) -> None:
    helper = tmp_path / "plugin-cache" / "sck_server"
    monkeypatch.setenv("GUIWEAVE_TEST_HELPER", str(helper))

    assert _helper_path("GUIWEAVE_TEST_HELPER", "fallback") == helper.resolve()


def test_iphone_apple_silicon_detection_handles_native_and_rosetta(
    monkeypatch,
) -> None:
    monkeypatch.setattr(iphone_factory.platform_module, "machine", lambda: "arm64")
    assert iphone_factory._is_apple_silicon()

    monkeypatch.setattr(iphone_factory.platform_module, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        iphone_factory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="1\n",
        ),
    )
    assert iphone_factory._is_apple_silicon()

    monkeypatch.setattr(
        iphone_factory.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="0\n",
        ),
    )
    assert not iphone_factory._is_apple_silicon()


def test_iphone_gatekeeper_assessment_blocks_quarantined_helper(
    tmp_path,
    monkeypatch,
) -> None:
    helper = tmp_path / "sck_server"
    helper.write_bytes(b"helper")

    def run(command, **_kwargs):
        if command[0] == "spctl":
            return SimpleNamespace(returncode=3, stdout="", stderr="rejected")
        assert command[:3] == ["xattr", "-p", "com.apple.quarantine"]
        return SimpleNamespace(returncode=0, stdout="0081;...", stderr="")

    monkeypatch.setattr(iphone_factory.subprocess, "run", run)

    status, detail = iphone_factory._gatekeeper_assessment(helper)

    assert status == "blocked"
    assert "签名并公证" in detail


def test_iphone_gatekeeper_assessment_marks_unquarantined_adhoc_helper_preview_only(
    tmp_path,
    monkeypatch,
) -> None:
    helper = tmp_path / "mirror_daemon"
    helper.write_bytes(b"helper")

    def run(command, **_kwargs):
        return SimpleNamespace(
            returncode=3 if command[0] == "spctl" else 1,
            stdout="",
            stderr="rejected",
        )

    monkeypatch.setattr(iphone_factory.subprocess, "run", run)

    status, detail = iphone_factory._gatekeeper_assessment(helper)

    assert status == "preview"
    assert "Developer ID" in detail


def test_iphone_preflight_blocks_helper_rejected_by_gatekeeper(
    tmp_path,
    monkeypatch,
) -> None:
    from gui_agent.adapters.iphone import client as iphone_client

    sck = tmp_path / "sck_server"
    daemon = tmp_path / "mirror_daemon"
    for helper in (sck, daemon):
        helper.write_bytes(b"helper")
        helper.chmod(0o755)
    monkeypatch.setattr(iphone_client, "SCK_SERVER", sck)
    monkeypatch.setattr(iphone_client, "MIRROR_DAEMON", daemon)
    monkeypatch.setattr(iphone_factory, "_is_apple_silicon", lambda: True)
    monkeypatch.setattr(
        iphone_factory,
        "mirroring_window_bounds",
        lambda: (0.0, 0.0, 318.0, 701.0),
    )
    monkeypatch.setattr(
        iphone_factory,
        "_gatekeeper_assessment",
        lambda path: (
            ("blocked", "quarantined helper rejected")
            if path == sck
            else ("accepted", "accepted")
        ),
    )

    result = iphone_factory._setup_check()

    assert not result.ok
    assert result.summary == "iPhone helper 被 macOS Gatekeeper 阻止"
    assert any("quarantined helper rejected" in line for line in result.lines)


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
