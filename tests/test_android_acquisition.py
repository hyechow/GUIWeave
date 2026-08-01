from types import SimpleNamespace

from gui_agent.adapters.android.acquisition import move_collection


class _Client:
    viewport_size = (1080, 2400)

    def __init__(self):
        self.calls = []

    def scroll(self, direction, amount, x, y):
        self.calls.append((direction, amount, x, y))
        return f"OK scroll {direction}"


def test_android_moves_only_the_bound_scroll_region() -> None:
    client = _Client()
    session = SimpleNamespace(client=client)
    table = {
        "traversal": {"type": "scroll"},
        "_region_bounds": [100.0, 200.0, 900.0, 800.0],
    }

    assert move_collection(session, table, "scroll_forward") is True
    assert client.calls == [("down", 4, 540.0, 1200.0)]
    assert move_collection(session, table, "paginate_next") is False
    assert len(client.calls) == 1
