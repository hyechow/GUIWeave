import os

from gui_agent.adapters.browser.device import (
    PlaywrightDevice,
    _CDPTimeout,
    _cdp_proxy_bypass,
    _direct_cdp_host,
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
        raise _CDPTimeout()

    dev._timed_cdp_send = timed_send

    dev._ensure_net_tracking()
    dev._ensure_net_tracking()

    assert calls == [(session, "Network.enable", {})]
    assert dev._net_session is session
    assert dev._xhr_ids == {}
    assert session.handlers == {}


def test_tracked_request_keeps_browser_loading_until_response_headers():
    session = _FakeSession()
    dev = _device_with_session(session)
    dev._timed_cdp_send = lambda *_args: {}
    dev._ensure_net_tracking()
    request = session.handlers["Network.requestWillBeSent"]
    response = session.handlers["Network.responseReceived"]

    request({"requestId": "save", "type": "XHR"})
    dev._cdp_send = lambda *_args, **_kwargs: {
        "result": {"value": "complete"}
    }
    assert dev.is_loading() is True

    response({"requestId": "save", "type": "XHR"})
    assert dev.is_loading() is False


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
