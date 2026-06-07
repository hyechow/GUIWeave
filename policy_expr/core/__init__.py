"""Platform-neutral core seam for iphone-use.

``policy_expr.core`` holds the single platform-neutral boundary
(:mod:`policy_expr.core.contracts`) that core orchestration depends on and that
iphone / browser / android adapters implement. Re-exported here for ergonomic
``from policy_expr.core import Device, ActionPolicy, ...`` imports.
"""

from __future__ import annotations

from policy_expr.core.contracts import (
    ActionPolicy,
    Device,
    FrontierEnumerator,
    KnowledgeAwareSupervisor,
    ObservationLike,
    Perception,
    PerceptionSession,
    ScrollableDevice,
    SimilarityBackend,
    StateDeduper,
    SupervisorPolicy,
    TransitionDetector,
    TreeObservationLike,
    ZeroPreemptDevice,
)

__all__ = [
    "Device",
    "ScrollableDevice",
    "ZeroPreemptDevice",
    "PerceptionSession",
    "Perception",
    "ObservationLike",
    "TreeObservationLike",
    "ActionPolicy",
    "SupervisorPolicy",
    "KnowledgeAwareSupervisor",
    "FrontierEnumerator",
    "StateDeduper",
    "TransitionDetector",
    "SimilarityBackend",
]
