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

    def dump_ui_hierarchy(self):
        self.hierarchy_calls += 1
        return next(self.hierarchy_results)


@pytest.mark.parametrize(("results", "calls"), [
    ([None, "<hierarchy><node /></hierarchy>"], 2),
    (["<hierarchy><node /></hierarchy>"], 1),
])
def test_android_capture_retries_only_a_missing_optional_hierarchy(
    results, calls,
) -> None:
    session = AndroidSession()
    session.client = _Client(results)

    assert session.capture() == (b"png", "<hierarchy><node /></hierarchy>")
    assert session.client.screenshot_calls == 1
    assert session.client.hierarchy_calls == calls
