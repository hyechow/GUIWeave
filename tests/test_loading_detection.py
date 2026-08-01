"""Tiered loading perception routes only ambiguous frames to vision."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from gui_agent.core.schemas import Observation
from gui_agent.core.vision.loading import (
    VisualLoadingDecision,
    assess_loading,
    heuristic_loading_assessment,
)


def _png(kind: str) -> bytes:
    image = Image.new("RGB", (540, 1200), (250, 249, 252))
    draw = ImageDraw.Draw(image)
    if kind == "splash":
        draw.ellipse((180, 430, 360, 610), fill=(88, 70, 225))
        draw.rounded_rectangle((230, 475, 310, 555), radius=18, outline="white", width=12)
    elif kind == "rendered":
        draw.rectangle((0, 0, 540, 100), fill=(35, 38, 48))
        for index in range(9):
            y = 140 + index * 100
            draw.ellipse((24, y, 76, y + 52), fill=(70, 90, 180))
            draw.rectangle((100, y + 4, 420, y + 18), fill=(45, 45, 50))
            draw.rectangle((100, y + 30, 350, y + 42), fill=(125, 125, 130))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_platform_signal_is_authoritative_without_visual_call() -> None:
    calls = 0

    def classifier(_png_bytes: bytes) -> VisualLoadingDecision:
        nonlocal calls
        calls += 1
        raise AssertionError("visual fallback must not run")

    platform = assess_loading(
        Observation(png_bytes=_png("splash"), source="android", loading=True),
        visual_classifier=classifier,
    )
    assert (platform.state, platform.source) == ("loading", "platform")
    assert calls == 0


def test_clear_pixel_endpoints_do_not_call_visual_fallback() -> None:
    calls = 0

    def classifier(_png_bytes: bytes) -> VisualLoadingDecision:
        nonlocal calls
        calls += 1
        raise AssertionError("visual fallback must not run")

    blank = assess_loading(
        Observation(png_bytes=_png("blank"), source="android"),
        visual_classifier=classifier,
    )
    rendered = assess_loading(
        Observation(png_bytes=_png("rendered"), source="android"),
        visual_classifier=classifier,
    )

    assert (blank.state, blank.source) == ("loading", "heuristic")
    assert (rendered.state, rendered.source) == ("rendered", "heuristic")
    assert calls == 0


def test_sparse_nonblank_frame_routes_to_visual_fallback() -> None:
    observation = Observation(png_bytes=_png("splash"), source="android")
    heuristic = heuristic_loading_assessment(observation)
    calls = 0

    def classifier(_png_bytes: bytes) -> VisualLoadingDecision:
        nonlocal calls
        calls += 1
        return VisualLoadingDecision(
            state="loading",
            confidence="high",
            evidence="only a centered brand mark is visible",
        )

    result = assess_loading(observation, visual_classifier=classifier)

    assert heuristic.state == "uncertain"
    assert (result.state, result.source) == ("loading", "vlm")
    assert calls == 1


def test_sparse_frame_with_ahead_of_surface_structure_still_uses_vision() -> None:
    observation = Observation(
        png_bytes=_png("splash"),
        source="android",
        semantic_tree=[
            {"role": "text", "key": "Home"},
            {"role": "button", "key": "Favorite"},
        ],
    )
    calls = 0

    def classifier(_png_bytes: bytes) -> VisualLoadingDecision:
        nonlocal calls
        calls += 1
        return VisualLoadingDecision(
            state="loading",
            confidence="high",
            evidence="a splash overlay still covers the structured app content",
        )

    heuristic = heuristic_loading_assessment(observation)
    result = assess_loading(observation, visual_classifier=classifier)

    assert heuristic.state == "uncertain"
    assert "structure exists" in heuristic.reason
    assert (result.state, result.source) == ("loading", "vlm")
    assert calls == 1


def test_visually_blank_but_rendered_structure_can_be_resolved_as_rendered() -> None:
    observation = Observation(
        png_bytes=_png("blank"),
        source="android",
        semantic_tree=[
            {"role": "textbox", "key": "#dogs", "value": "#dogs"},
            {"role": "listitem", "key": "Posts with #dogs"},
        ],
    )
    calls = 0

    def classifier(_png_bytes: bytes) -> VisualLoadingDecision:
        nonlocal calls
        calls += 1
        return VisualLoadingDecision(
            state="rendered",
            confidence="high",
            evidence="interactive search results are already visible",
        )

    heuristic = heuristic_loading_assessment(observation)
    result = assess_loading(observation, visual_classifier=classifier)

    assert heuristic.state == "uncertain"
    assert (result.state, result.source) == ("rendered", "vlm")
    assert calls == 1


def test_visual_fallback_failure_preserves_previous_fail_open_behavior() -> None:
    result = assess_loading(
        Observation(png_bytes=_png("splash"), source="android"),
        visual_classifier=lambda _png_bytes: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert (result.state, result.source) == ("rendered", "fallback")
