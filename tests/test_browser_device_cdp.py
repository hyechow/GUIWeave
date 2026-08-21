import json
import os
import signal
from types import SimpleNamespace

import pytest

from gui_agent.adapters.browser.device import (
    PlaywrightDevice,
    _cdp_proxy_bypass,
    _direct_cdp_host,
    _playwright_driver_interrupt_isolation,
)


class _FakeSession:
    def __init__(self):
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler):
        self.handlers[event] = handler


def _device_with_session(session: _FakeSession) -> PlaywrightDevice:
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._cdp = session
    dev._net_session = None
    dev._xhr_ids = {}
    dev._xhr_last = 0.0
    return dev


def test_cdp_proxy_bypass_is_scoped_to_driver_startup(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.delenv("no_proxy", raising=False)

    with _cdp_proxy_bypass("http://localhost:9222"):
        assert set(os.environ["NO_PROXY"].split(",")) >= {
            "example.test",
            "localhost",
            "127.0.0.1",
            "::1",
        }
        assert "localhost" in os.environ["no_proxy"].split(",")

    assert os.environ["NO_PROXY"] == "example.test"
    assert "no_proxy" not in os.environ


def test_playwright_driver_ignores_sigint_only_during_spawn() -> None:
    previous = signal.getsignal(signal.SIGINT)

    with _playwright_driver_interrupt_isolation():
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN

    assert signal.getsignal(signal.SIGINT) == previous


def test_close_skips_sync_calls_after_playwright_driver_exit() -> None:
    calls: list[str] = []
    process = SimpleNamespace(returncode=-signal.SIGINT)
    transport = SimpleNamespace(_proc=process)
    connection = SimpleNamespace(_transport=transport)
    playwright = SimpleNamespace(
        _impl_obj=SimpleNamespace(_connection=connection),
        stop=lambda: calls.append("stop"),
    )
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev.headless = True
    dev._pw = playwright
    dev._context = SimpleNamespace(close=lambda: calls.append("context"))
    dev._browser = SimpleNamespace(close=lambda: calls.append("browser"))
    dev.page = object()
    dev._cdp = object()
    dev._browser_cdp = object()
    dev._prev_pages = []
    dev._tab_switched = True
    dev._last_viewport = (1280, 800)
    dev._dpr = 1.0

    dev.close()

    assert calls == []
    assert dev._pw is None
    assert dev.page is None


def test_cdp_proxy_bypass_is_limited_to_local_and_private_hosts():
    assert _direct_cdp_host("http://localhost:9222") == "localhost"
    assert _direct_cdp_host("http://192.168.1.103:9222") == "192.168.1.103"
    assert _direct_cdp_host("https://public.example:9222") == ""


def test_ensure_net_tracking_uses_timed_send_and_arms_handlers():
    session = _FakeSession()
    dev = _device_with_session(session)
    calls = []

    def timed_send(sess, method, params):
        calls.append((sess, method, params))
        return {}

    dev._timed_cdp_send = timed_send

    dev._ensure_net_tracking()

    assert calls == [(session, "Network.enable", {})]
    assert dev._net_session is session
    assert list(session.handlers) == [
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
    ]


def test_ensure_net_tracking_timeout_degrades_without_retrying_same_session():
    session = _FakeSession()
    dev = _device_with_session(session)
    dev._xhr_ids = {"stuck": 1.0}
    calls = []

    def timed_send(sess, method, params):
        calls.append((sess, method, params))
        raise TimeoutError()

    dev._timed_cdp_send = timed_send

    dev._ensure_net_tracking()
    dev._ensure_net_tracking()

    assert calls == [(session, "Network.enable", {})]
    assert dev._net_session is session
    assert dev._xhr_ids == {}
    assert session.handlers == {}


def test_tracked_request_does_not_override_document_readiness():
    session = _FakeSession()
    dev = _device_with_session(session)
    dev._timed_cdp_send = lambda *_args: {}
    dev._ensure_net_tracking()
    request = session.handlers["Network.requestWillBeSent"]
    response = session.handlers["Network.responseReceived"]

    request({"requestId": "save", "type": "XHR"})
    dev._cdp_send = lambda *_args, **_kwargs: {
        "result": {"value": ["complete", True]}
    }
    assert dev.is_loading() is False

    response({"requestId": "save", "type": "XHR"})
    assert dev.is_loading() is False


def test_empty_semantic_main_remains_loading_after_document_complete():
    dev = _device_with_session(_FakeSession())
    dev._cdp_send = lambda *_args, **_kwargs: {
        "result": {"value": ["complete", False]}
    }

    assert dev.is_loading() is True


def test_settle_waits_for_content_but_not_background_requests():
    dev = _device_with_session(_FakeSession())
    dev._xhr_ids = {"background": 1.0}
    dev._ensure_net_tracking = lambda: None
    probes = iter((["complete", 500, False], ["complete", 500, True]))
    probe_count = []

    def cdp_send(_method, params):
        if "return 1" in params["expression"]:
            value = 1
        else:
            probe_count.append(True)
            value = next(probes)
        return {"result": {"value": value}}

    dev._cdp_send = cdp_send

    dev.wait_settled("navigate")

    assert len(probe_count) == 2


def test_cdp_navigation_surfaces_protocol_error_text():
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev.headless = False
    dev._native_action_feedback = []
    dev._cdp_send = lambda *_args, **_kwargs: {"errorText": "net::ERR_TIMED_OUT"}

    result = dev.navigate("https://example.test/")

    assert result == (
        "failed: navigate https://example.test/: net::ERR_TIMED_OUT"
    )
    assert json.loads(dev._native_action_feedback[0]["body"]) == {
        "error": True,
        "message": "net::ERR_TIMED_OUT",
    }


def test_headless_navigation_uses_bounded_commit_wait():
    calls = []
    page = SimpleNamespace(
        goto=lambda url, **options: calls.append((url, options)),
    )
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev.headless = True
    dev._native_action_feedback = []
    dev._require_page = lambda: page

    assert dev.navigate("https://example.test/") == "OK navigate https://example.test/"
    assert calls == [(
        "https://example.test/",
        {"wait_until": "commit", "timeout": 5_000},
    )]


def test_select_option_uses_rendered_radio_group_target() -> None:
    clicks = []

    class Page:
        mouse = SimpleNamespace(click=lambda x, y: clicks.append((x, y)))

        @staticmethod
        def bring_to_front():
            return None

        @staticmethod
        def evaluate(script, args):
            assert 'input[type="radio"]' in script
            assert "radio.name === anchorRadio.name" in script
            assert "document.elementFromPoint(point.x, point.y)" in script
            assert "byPoint?.closest?." in script
            assert "return mouseTarget(pointedOption)" in script
            assert args == {
                "x": 180, "y": 519, "target": "19", "deselect": False,
                "controlId": "",
            }
            return {"ok": True, "mode": "mouse", "x": 234, "y": 519, "label": "4 stars"}

    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._follow_active_tab = lambda: None
    dev._require_page = lambda: Page()

    assert dev.select_option(180, 519, "19") == "OK select_option '4 stars' (mouse)"
    assert clicks == [(234.0, 519.0)]


def test_select_option_transports_offscreen_control_by_runtime_id() -> None:
    captured = {}

    class Page:
        mouse = SimpleNamespace(click=lambda *_args: None)

        @staticmethod
        def bring_to_front():
            return None

        @staticmethod
        def evaluate(script, args):
            captured.update(args)
            assert "getElementById(controlId)" in script
            assert "scrollIntoView" in script
            return {"ok": True, "mode": "native", "label": "36", "value": "36"}

    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._follow_active_tab = lambda: None
    dev._require_page = lambda: Page()
    dev._dismiss_choice_overlay = lambda: None

    assert dev.select_option(
        900, 999, "36", control_id="limiter",
    ) == "OK select_option '36' (select) value='36'"
    assert captured["controlId"] == "limiter"


def test_new_tab_snapshots_the_explicitly_created_page() -> None:
    page = object()
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._context = SimpleNamespace(new_page=lambda: page)
    dev._switch_page = lambda selected: setattr(dev, "page", selected)
    dev.navigate = lambda url: f"OK navigate {url}"
    dev._all_pages = lambda: [page]
    dev._prev_pages = []

    assert dev.new_tab("https://example.test/") == (
        "OK new_tab; OK navigate https://example.test/"
    )
    assert dev._prev_pages == [page]


def test_select_tab_next_cycles_through_open_pages() -> None:
    first = SimpleNamespace(
        url="https://first.test", context=object(), bring_to_front=lambda: None,
    )
    second = SimpleNamespace(
        url="https://second.test", context=object(), bring_to_front=lambda: None,
    )
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev.page = first
    dev._cdp = None
    dev._tab_switched = False
    dev._all_pages = lambda: [first, second]
    dev._target_meta = lambda: {second.url: "Second"}

    assert dev.select_tab("next").startswith("OK select_tab 'Second'")
    assert dev.page is second
    assert dev.select_tab("next").startswith("OK select_tab ''")
    assert dev.page is first
    assert dev.select_tab("next").startswith("failed: 已遍历全部标签页")
    assert dev.page is first
    assert dev.select_tab("Second").startswith("OK select_tab 'Second'")
    assert dev.tab_cycle_finalized is True
    assert dev.select_tab("next").startswith("failed: 已遍历全部标签页")


def test_failed_start_navigation_stops_before_first_observation() -> None:
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev.start_url = "https://unreachable.example/"
    dev.navigate = lambda url: f"failed: navigate {url}: net::ERR_TIMED_OUT"

    with pytest.raises(RuntimeError, match="browser start navigation failed"):
        dev._navigate_to_start_url()


def test_navigation_failure_feedback_skips_unresponsive_page_probe():
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev.headless = True
    dev._native_action_feedback = []
    dev._require_page = lambda: SimpleNamespace(
        goto=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("navigation timed out\nCall log:\n  - internal detail")
        )
    )
    stop_calls = []
    dev._cdp_send = lambda method, params: stop_calls.append((method, params)) or {}

    result = dev.navigate("https://example.test/")
    assert "navigation timed out" in result
    assert "internal detail" not in result
    assert stop_calls == [("Page.stopLoading", {})]
    dev._follow_active_tab = lambda: None
    dev._cdp_send = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("native navigation failure must bypass the page probe")
    )
    feedback = dev.consume_action_feedback()

    assert len(feedback) == 1
    assert json.loads(feedback[0]["body"])["message"] == "navigation timed out"


def test_browser_scroll_is_capped_below_one_viewport() -> None:
    wheel_calls = []
    page = SimpleNamespace(mouse=SimpleNamespace(
        move=lambda *_args: None,
        wheel=lambda dx, dy: wheel_calls.append((dx, dy)),
    ))
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._last_viewport = (1280, 800)
    dev._follow_active_tab = lambda: None
    dev._require_page = lambda: page

    result = dev.scroll("down", amount=9, x=640, y=400)

    assert wheel_calls == [(0, 720)]
    assert "720px" in result


def test_dom_snap_text_retarget_uses_interactive_accessible_names():
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    captured: dict[str, str] = {}

    def cdp_send(_method, params):
        captured["expression"] = params["expression"]
        return {"result": {"value": ""}}

    dev._cdp_send = cdp_send

    assert dev.dom_snap(146, 435, target_text="size") == (146, 435, None)
    expression = captured["expression"]
    assert "const RETARGET=" in expression
    assert "const accessibleName=" in expression
    assert "e.matches(RETARGET)" in expression
    assert "accessibleName(e)===norm(target)" in expression
    assert "const SEARCH=RETARGET+',input,select,textarea'" in expression
    assert "if(!textMatch(c) && !matchCtl(c))continue" in expression
