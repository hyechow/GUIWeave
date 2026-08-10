"""Deterministic repeated-action fuse for one autonomous GUI Worker."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from gui_agent.core.tool_agent.contracts import MaterializedFrame


_GUARDED_CAPABILITIES = {
    "tap",
    "type",
    "clear_text",
    "press_enter",
    "select_option",
    "scroll",
    "open_url",
    "back",
}
_SIGNATURE_FIELDS = (
    "text",
    "url",
    "direction",
    "amount",
    "target_area",
)


def _coordinate_bucket(value: Any, *, size: int = 25) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return int(round(float(value) / size) * size)


def action_signature(
    *,
    tool: str,
    capability: str,
    args: dict[str, Any],
) -> str:
    """Return a semantic signature tolerant of a few pixels of coordinate jitter."""
    payload: dict[str, Any] = {
        "tool": tool,
        "capability": capability,
    }
    for field_name in _SIGNATURE_FIELDS:
        value = args.get(field_name)
        if value not in (None, ""):
            payload[field_name] = value
    for coordinate in ("x", "y"):
        bucket = _coordinate_bucket(args.get(coordinate))
        if bucket is not None:
            payload[coordinate] = bucket
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def progress_signature(frame: MaterializedFrame) -> str:
    """Hash task-relevant progress while ignoring unrelated visual mutations."""
    payload = {
        "url": frame.url,
        "title": frame.title,
        "scopes": {
            key: {
                "status": value.get("status"),
                "applied_filters": value.get("applied_filters"),
            }
            for key, value in sorted(frame.requirement_scopes.items())
        },
        "collections": [
            {
                "requirement_id": item.requirement_id,
                "row_count": item.row_count,
                "status": item.coverage.get("status"),
                "pages_seen": item.coverage.get("pages_seen"),
            }
            for item in frame.collections
        ],
    }
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(rendered.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ActionCircuitDecision:
    blocked: bool
    signature: str
    progress: str
    prior_attempts: int
    reason: str = ""


@dataclass
class WorkerActionCircuitBreaker:
    """Block a third equivalent action in the same task-progress window."""

    threshold: int = 2
    _attempts: dict[tuple[str, str], int] = field(default_factory=dict)

    def inspect(
        self,
        *,
        tool: str,
        capability: str,
        args: dict[str, Any],
        frame: MaterializedFrame,
    ) -> ActionCircuitDecision:
        signature = action_signature(tool=tool, capability=capability, args=args)
        progress = progress_signature(frame)
        prior = self._attempts.get((signature, progress), 0)
        guarded = capability in _GUARDED_CAPABILITIES
        blocked = guarded and prior >= self.threshold
        reason = ""
        if blocked:
            reason = (
                f"blocked repeated {capability} action after {prior} equivalent "
                "dispatches without task-relevant progress"
            )
        return ActionCircuitDecision(
            blocked=blocked,
            signature=signature,
            progress=progress,
            prior_attempts=prior,
            reason=reason,
        )

    def record(self, decision: ActionCircuitDecision) -> None:
        key = (decision.signature, decision.progress)
        self._attempts[key] = self._attempts.get(key, 0) + 1


__all__ = [
    "ActionCircuitDecision",
    "WorkerActionCircuitBreaker",
    "action_signature",
    "progress_signature",
]
