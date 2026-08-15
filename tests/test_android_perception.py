import pytest

from gui_agent.adapters.android.perception import AndroidPerception, AndroidSession


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


def test_android_observation_includes_webview_document(tmp_path) -> None:
    class _WebViewClient:
        viewport_size = (1080, 2400)

        def webview_document(self):
            return {
                "url": "content://notes",
                "title": "notes.txt",
                "tables": [{"caption": "notes.txt", "rows": [{"Content": ["one"]}]}],
            }

    class _Session:
        client = _WebViewClient()

        def capture(self):
            return (
                b"not-a-png",
                '<hierarchy><node class="android.webkit.WebView">'
                '<node class="android.widget.TextView" text="document body" />'
                '</node></hierarchy>',
            )

    observation = AndroidPerception(
        _Session(), screenshot_path=tmp_path / "screen.png",
    ).observe()

    assert observation.url == "content://notes"
    assert observation.title == "notes.txt"
    assert observation.tables == [{"caption": "notes.txt", "rows": [{"Content": ["one"]}]}]


def test_android_observation_probes_webview_when_hierarchy_is_unavailable(tmp_path) -> None:
    class _WebViewClient:
        viewport_size = (1080, 2400)

        def webview_document(self):
            return {"title": "notes.txt", "tables": []}

    class _Session:
        client = _WebViewClient()

        def capture(self):
            return b"not-a-png", None

    observation = AndroidPerception(
        _Session(), screenshot_path=tmp_path / "screen.png",
    ).observe()

    assert observation.title == "notes.txt"


def test_android_observation_skips_webview_probe_on_native_surface(tmp_path) -> None:
    class _NativeClient:
        viewport_size = (1080, 2400)

        def webview_document(self):
            raise AssertionError("native hierarchy must not probe CDP")

    class _Session:
        client = _NativeClient()

        def capture(self):
            return b"not-a-png", "<hierarchy><node /></hierarchy>"

    observation = AndroidPerception(
        _Session(), screenshot_path=tmp_path / "screen.png",
    ).observe()

    assert observation.url is None
