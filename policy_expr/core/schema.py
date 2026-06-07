"""Platform-neutral data types shared across the core seam.

Pure data shapes (dataclasses / a plain exception) with no pixel, model, or
platform state, lifted out of the iPhone recon adapter (S3) so that
policy_expr.core.contracts and core orchestration can reference them without a
forward-ref into any adapter. The former adapter locations re-export these for
backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass


class ProbeAbortedError(RuntimeError):
    """Raised when probe_elements cannot return to initial page after a tap."""

    def __init__(
        self,
        message: str,
        failed_tap: int,
        failed_element: str,
        back_attempts: list[dict],
    ):
        super().__init__(message)
        self.failed_tap = failed_tap
        self.failed_element = failed_element
        self.back_attempts = back_attempts  # list of {strategy, coords, score, success}


@dataclass
class IdentityResult:
    is_duplicate: bool
    reason: str
    best_visual_sim: float = 0.0
    best_text_sim: float | None = None
    matched_index: int | None = None
    matched_name: str | None = None
    library_size: int = 0
    phase: str = ""  # "visual_shortcut" / "semantic_match" / "new_page"


@dataclass(frozen=True)
class ScreenMatchDecision:
    """Layered decision for whether a screen matches the initial page."""

    matched: bool | None
    similarity: float
    method: str
    reason: str
