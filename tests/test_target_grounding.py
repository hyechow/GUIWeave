from __future__ import annotations

import io

import pytest
from PIL import Image

from gui_agent.adapters.android.actions import AndroidAction
from gui_agent.core.schemas import (
    BaseActionDecision,
    TargetGrounding,
)
from gui_agent.core.vision.target_verify import (
    crop_target_region,
    resolve_target_grounding,
)


def _decision(*, action_type: str = "type", x: float = 500, y: float = 160):
    return BaseActionDecision(action=AndroidAction(
        action_type=action_type,
        x=x,
        y=y,
        text="value" if action_type == "type" else None,
        description="Target control",
    ))


def test_target_grounding_requires_valid_normalized_box() -> None:
    with pytest.raises(ValueError, match="positive width"):
        TargetGrounding(
            target_found=True,
            target_box=(500, 100, 400, 200),
            confidence="high",
        )
    with pytest.raises(ValueError, match="requires target_box"):
        TargetGrounding(target_found=True, confidence="high")


@pytest.mark.parametrize(
    ("action_type", "point", "box", "kind", "confidence", "expected", "allowed"),
    [
        ("type", (500, 160), (200, 100, 800, 150), "text_input", "high", (500, 125), True),
        ("tap", (450, 500), (400, 400, 600, 600), "button", "high", (450, 500), True),
        ("type", (500, 140), (400, 100, 600, 160), "button", "high", (500, 140), False),
        ("tap", (100, 100), (400, 200, 500, 280), "button", "high", (450, 240), True),
        ("tap", (100, 100), (400, 200, 500, 280), "button", "medium", (100, 100), False),
    ],
)
def test_target_grounding_resolution(
    action_type: str,
    point: tuple[float, float],
    box: tuple[float, float, float, float],
    kind: str,
    confidence: str,
    expected: tuple[float, float],
    allowed: bool,
) -> None:
    grounding = TargetGrounding(
        target_found=True,
        target_box=box,
        control_type=kind,
        label="Target",
        confidence=confidence,
    )
    original = _decision(action_type=action_type, x=point[0], y=point[1])

    grounded, signal, _, error = resolve_target_grounding(original, grounding)

    assert (grounded.action.x, grounded.action.y) == expected
    assert bool(signal) is allowed
    assert bool(error) is not allowed
    if expected != point:
        assert grounded.action.snap["method"] == "visual_target_grounding"


def test_type_into_undetected_region_is_admitted_for_red_marker_verifier() -> None:
    """A `type` over an undetected region (no control found) is not hard-rejected.

    A blank composer body has no detected element to conflict with; the point is
    admitted and left to the downstream red-marker verifier. A detected non-text
    control must still block the type.
    """
    grounding = TargetGrounding(
        target_found=False,
        control_type="",
        label="",
        confidence="low",
    )
    grounded, signal, _, error = resolve_target_grounding(
        _decision(action_type="type", x=500, y=160), grounding
    )
    assert signal is None
    assert error == ""
    assert (grounded.action.x, grounded.action.y) == (500, 160)


def test_unidentified_outside_tap_does_not_override_visual_point() -> None:
    grounding = TargetGrounding(
        target_found=True,
        target_box=(400, 200, 500, 280),
        control_type="button",
        label="",
        container_context="",
        confidence="high",
        reason="A nearby unlabeled icon may be the target.",
    )

    grounded, signal, inside, error = resolve_target_grounding(
        _decision(action_type="tap", x=100, y=100), grounding
    )

    assert inside is False
    assert signal is None
    assert error == ""
    assert (grounded.action.x, grounded.action.y) == (100, 100)
    assert grounded.action.snap is None


def test_candidate_crop_preserves_expected_local_region() -> None:
    image = Image.new("RGB", (360, 800), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")

    cropped, bounds = crop_target_region(output.getvalue(), 500, 160)
    cropped_image = Image.open(io.BytesIO(cropped))
    assert bounds == (100, 0, 900, 340)
    assert cropped_image.size == (288, 272)
