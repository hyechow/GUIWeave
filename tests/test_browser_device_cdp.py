from gui_agent.adapters.browser.device import PlaywrightDevice, _CDPTimeout


class _FakeSession:
    def __init__(self):
        self.handlers: list[str] = []

    def on(self, event: str, handler):
        self.handlers.append(event)


def _device_with_session(session: _FakeSession) -> PlaywrightDevice:
    dev = PlaywrightDevice.__new__(PlaywrightDevice)
    dev._cdp = session
    dev._net_session = None
    dev._xhr_ids = {}
    dev._xhr_last = 0.0
    return dev


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
    assert session.handlers == [
        "Network.requestWillBeSent",
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
    assert session.handlers == []
