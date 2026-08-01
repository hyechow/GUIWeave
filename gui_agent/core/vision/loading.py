"""Tiered loading-state perception for GUI observations.

Explicit platform facts take precedence. Conservative visual heuristics classify only
clear endpoints; structure can corroborate a rendered frame but cannot override a sparse
visible surface. A small vision model handles the ambiguous middle instead of turning
another image threshold into a runtime truth.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import Observation
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from .frame_analysis import is_blank_screen

LoadingState = Literal["loading", "rendered", "uncertain"]
LoadingSource = Literal["platform", "structure", "heuristic", "vlm", "fallback"]


@dataclass(frozen=True)
class LoadingAssessment:
    state: LoadingState
    source: LoadingSource
    reason: str

    @property
    def is_loading(self) -> bool:
        return self.state == "loading"


class VisualLoadingDecision(BaseModel):
    """Minimal VLM fallback result for one visually ambiguous frame."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["loading", "rendered"]
    confidence: Literal["high", "medium", "low"]
    evidence: str = Field(min_length=1)


VisualClassifier = Callable[[bytes], VisualLoadingDecision]


def heuristic_loading_assessment(observation: Observation) -> LoadingAssessment:
    """Return a high-confidence endpoint, or ``uncertain`` for visual fallback."""
    if observation.loading is not None:
        return LoadingAssessment(
            state="loading" if observation.loading else "rendered",
            source="platform",
            reason="platform supplied an authoritative loading signal",
        )
    visually_blank = is_blank_screen(observation.png_bytes)
    rendered_structure = _has_rendered_structure(observation)
    if visually_blank and not rendered_structure:
        return LoadingAssessment(
            state="loading",
            source="heuristic",
            reason="the content body is light and nearly uniform with no rendered structure",
        )

    if visually_blank:
        return LoadingAssessment(
            state="uncertain",
            source="heuristic",
            reason=(
                "the visible body appears blank while current-frame structure exposes "
                "rendered content"
            ),
        )

    edge_density, entropy, background_ratio = _visual_complexity(
        observation.png_bytes
    )
    if edge_density >= 0.04 and entropy >= 2.0 and background_ratio <= 0.88:
        return LoadingAssessment(
            state="rendered",
            source="structure" if rendered_structure else "heuristic",
            reason=(
                "the visible frame has dense, varied content"
                f"{' backed by platform structure' if rendered_structure else ''} "
                f"(edges={edge_density:.3f}, entropy={entropy:.2f})"
            ),
        )
    structure_note = (
        "; platform structure exists but may be ahead of the visible surface"
        if rendered_structure
        else ""
    )
    return LoadingAssessment(
        state="uncertain",
        source="heuristic",
        reason=(
            "the frame is sparse but not blank "
            f"(edges={edge_density:.3f}, entropy={entropy:.2f}, "
            f"background={background_ratio:.3f}){structure_note}"
        ),
    )


def assess_loading(
    observation: Observation,
    *,
    visual_classifier: VisualClassifier | None = None,
) -> LoadingAssessment:
    """Resolve loading state through platform → heuristic → VLM tiers."""
    assessment = heuristic_loading_assessment(observation)
    if assessment.state != "uncertain":
        return assessment
    classifier = visual_classifier or _classify_visual_cached
    try:
        decision = classifier(observation.png_bytes)
    except Exception as exc:  # classifier failure must not create an infinite wait
        print(f"  [LoadingVLM] unavailable, treating frame as rendered: {exc}")
        return LoadingAssessment(
            state="rendered",
            source="fallback",
            reason=f"visual fallback unavailable: {type(exc).__name__}",
        )
    print(
        f"  [LoadingVLM] state={decision.state} confidence={decision.confidence}: "
        f"{decision.evidence}"
    )
    return LoadingAssessment(
        state=decision.state,
        source="vlm",
        reason=decision.evidence,
    )


def is_loading_frame(observation: Observation) -> bool:
    """Compatibility-free runtime predicate backed by the tiered assessment."""
    return assess_loading(observation).is_loading


def _has_rendered_structure(observation: Observation) -> bool:
    if any(
        getattr(region, "cells", ())
        for region in getattr(observation, "collection_regions", None) or ()
    ):
        return True
    if observation.tables or observation.form_controls:
        return True
    content_roles = {
        "button", "cell", "checkbox", "heading", "link", "listitem",
        "menuitem", "radio", "switch", "tab", "text", "textbox",
    }
    return any(
        str(node.get("role") or "").casefold() in content_roles
        and bool(str(node.get("key") or node.get("value") or "").strip())
        for node in observation.semantic_tree or ()
    )


def _visual_complexity(png_bytes: bytes) -> tuple[float, float, float]:
    """Return edge density, grayscale entropy, and dominant-background ratio."""
    import numpy as np

    image = Image.open(io.BytesIO(png_bytes)).convert("L").resize((128, 256))
    pixels = np.asarray(image, dtype=np.int16)[10:-10]
    dx = np.abs(np.diff(pixels, axis=1))
    dy = np.abs(np.diff(pixels, axis=0))
    edge_density = float(((dx > 18).mean() + (dy > 18).mean()) / 2)
    histogram = np.bincount(pixels.ravel(), minlength=256)
    probabilities = histogram[histogram > 0] / pixels.size
    entropy = float(-sum(value * math.log2(value) for value in probabilities))
    mode = int(histogram.argmax())
    background_ratio = float(
        histogram[max(0, mode - 5):min(256, mode + 6)].sum() / pixels.size
    )
    return edge_density, entropy, background_ratio


def _loading_llm() -> ChatOpenAI:
    config = resolve_llm_config("loading")
    return ChatOpenAI(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_s,
        max_retries=config.max_retries,
        temperature=0,
    )


def _prepare_png(png_bytes: bytes, max_width: int = 768) -> bytes:
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if image.width <= max_width:
        return png_bytes
    height = max(1, round(image.height * max_width / image.width))
    image = image.resize((max_width, height), Image.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@lru_cache(maxsize=32)
def _classify_visual_cached(png_bytes: bytes) -> VisualLoadingDecision:
    prepared = _prepare_png(png_bytes)
    encoded = base64.b64encode(prepared).decode()
    messages = [
        SystemMessage(content=load_prompt_text("task.vision.loading")),
        HumanMessage(content=[
            {"type": "text", "text": "判断当前 GUI 截图是否仍是临时加载画面。"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            },
        ]),
    ]
    return invoke_structured(
        _loading_llm(),
        messages,
        VisualLoadingDecision,
        trace_label="loading",
    )


__all__ = [
    "LoadingAssessment",
    "VisualLoadingDecision",
    "assess_loading",
    "heuristic_loading_assessment",
    "is_loading_frame",
]
