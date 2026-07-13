from __future__ import annotations

import io

from PIL import Image

from gui_agent.adapters.browser.page_read import scroll_until_read
from gui_agent.core.schemas import Observation


def _png(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(output, format="PNG")
    return output.getvalue()


class _Perception:
    def __init__(self, observation: Observation):
        self.observation = observation

    def observe(self) -> Observation:
        return self.observation


class _Bundle:
    def __init__(self, observations: list[Observation]):
        self.observations = list(observations)
        self.observed = 0

    def make_perception(self, platform, path):
        self.observed += 1
        return _Perception(self.observations.pop(0))


class _VisualClient:
    def __init__(self):
        self.scrolls = 0

    def scroll(self, direction, amount, x, y):
        self.scrolls += 1


class _Platform:
    def __init__(self, client):
        self.client = client


def test_visual_read_uses_shared_no_progress_boundary(monkeypatch, tmp_path) -> None:
    frame = _png((20, 30, 40))
    observations = [
        Observation(png_bytes=frame, source="test"),
        Observation(png_bytes=frame, source="test"),
        Observation(png_bytes=frame, source="test"),
    ]
    bundle = _Bundle(observations)
    client = _VisualClient()
    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.structured_read.structured_read",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr("gui_agent.adapters.browser.page_read.time.sleep", lambda _: None)

    result = scroll_until_read(
        bundle,
        _Platform(client),
        tmp_path,
        ["missing"],
        max_scrolls=4,
    )

    assert result == {}
    assert client.scrolls == 1
    assert bundle.observed == 3
