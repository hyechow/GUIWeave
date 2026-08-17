from gui_agent.adapters.android.webview import (
    _candidate_sockets,
    _document_snapshot,
    _visible_pages,
    read_foreground_document,
)


def test_webview_document_preserves_text_lines() -> None:
    snapshot = _document_snapshot(
        {"title": "notes.txt", "url": "content://notes"},
        {"title": "", "content": "first\n\nthird\n"},
    )

    assert snapshot is not None
    assert snapshot["tables"][0]["rows"] == [{"Content": "first\n\nthird\n"}]
    assert snapshot["tables"][0]["partial"] is False


def test_webview_candidates_and_targets_stay_on_foreground_surface() -> None:
    unix_table = """
@webview_devtools_remote_111
@webview_devtools_remote_222
@com.example.notes_devtools_remote
"""
    assert _candidate_sockets(unix_table, "com.example.notes", {"111"}) == [
        "webview_devtools_remote_111",
        "com.example.notes_devtools_remote",
    ]
    targets = [
        {"type": "page", "webSocketDebuggerUrl": "ws://hidden", "description": '{"visible":false}'},
        {"type": "page", "webSocketDebuggerUrl": "ws://visible", "description": '{"visible":true}'},
    ]
    assert _visible_pages(targets) == [targets[1]]


def test_read_foreground_document_cleans_up_forward(monkeypatch) -> None:
    calls = []

    class _Dev:
        def shell(self, command):
            return "111" if command.startswith("pidof") else "@webview_devtools_remote_111"

        def forward(self, local, remote, norebind=False):
            calls.append(("forward", local, remote, norebind))

        def forward_remove(self, local, raise_non_found=True):
            calls.append(("remove", local, raise_non_found))

    def get_json(url, **_kwargs):
        if url.endswith("/json/version"):
            return {"Android-Package": "com.example.notes"}
        return [{
            "type": "page",
            "title": "notes.txt",
            "url": "content://notes",
            "webSocketDebuggerUrl": "ws://localhost/devtools/page/1",
        }]

    monkeypatch.setattr("gui_agent.adapters.android.webview._get_json", get_json)
    monkeypatch.setattr(
        "gui_agent.adapters.android.webview._evaluate",
        lambda _url, **_kwargs: {"content": "one\ntwo\n"},
    )

    snapshot = read_foreground_document(_Dev(), "com.example.notes")

    assert snapshot is not None
    assert snapshot["tables"][0]["rows"][0]["Content"] == "one\ntwo\n"
    assert calls[0][0] == "forward"
    assert calls[-1][0] == "remove"
