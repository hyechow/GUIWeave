import pytest

from gui_agent.adapters.android.perception import AndroidSession


class _Client:
    def __init__(self, hierarchy_results):
        self.hierarchy_results = iter(hierarchy_results)
        self.screenshot_calls = 0
        self.hierarchy_calls = 0

    def screenshot(self):
        self.screenshot_calls += 1
        return b"png"

    def screenshot_once(self):
        self.screenshot_calls += 1
        return b"settle-png"

    def dump_ui_hierarchy(self, timeout_s=6.0):
        self.hierarchy_timeout_s = timeout_s
        self.hierarchy_calls += 1
        return next(self.hierarchy_results)


@pytest.mark.parametrize("result", [None, "<hierarchy><node /></hierarchy>"])
def test_android_capture_bounds_optional_hierarchy_to_one_attempt(result) -> None:
    session = AndroidSession()
    session.client = _Client([result])

    assert session.capture() == (b"png", result)
    assert session.client.screenshot_calls == 1
    assert session.client.hierarchy_calls == 1
    assert session.client.hierarchy_timeout_s == 6.0
    assert session.last_capture_timing["hierarchy_available"] is (result is not None)


def test_android_settle_screenshot_uses_one_primary_capture() -> None:
    session = AndroidSession()
    session.client = _Client(["<hierarchy />"])

    assert session.settle_screenshot() == b"settle-png"
    assert session.client.screenshot_calls == 1
