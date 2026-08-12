"""Deterministic action guards for one autonomous GUI Worker."""

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
    "drag",
    "long_press",
    "open_url",
    "back",
    "home",
    "app_switch",
    "launch_app",
}
_SIGNATURE_FIELDS = (
    "text",
    "url",
    "app",
    "direction",
    "amount",
    "duration_ms",
    "target_area",
)
def _control_at_point(args: dict[str, Any], frame: MaterializedFrame) -> dict[str, Any] | None:
    """Return the smallest visible enhanced control containing the action point."""

    x, y = args.get("x"), args.get("y")
    if not all(isinstance(value, (int, float)) for value in (x, y)):
        return None
    matches: list[tuple[float, dict[str, Any]]] = []
    for control in frame.controls:
        rect = control.get("rect")
        if (
            control.get("in_viewport") is False
            or not isinstance(rect, dict)
            or not all(isinstance(rect.get(key), (int, float)) for key in ("x", "y"))
        ):
            continue
        cx, cy = float(rect["x"]), float(rect["y"])
        width, height = float(rect.get("w") or 0), float(rect.get("h") or 0)
        if abs(float(x) - cx) <= width / 2 and abs(float(y) - cy) <= height / 2:
            matches.append((max(1.0, width * height), control))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _action_boundary_error(
    capability: str,
    args: dict[str, Any],
    frame: MaterializedFrame,
) -> str:
    """Reject actions contradicted by authoritative enhanced observation."""

    control = _control_at_point(args, frame)
    if control is not None:
        kind = str(control.get("kind") or "").casefold()
        label = str(control.get("label") or kind or "control")
        text_like = any(
            token in kind for token in ("input", "textarea", "textbox", "editor")
        )
        choice_like = any(
            key in control for key in ("options", "selected_text", "selected_text_primary")
        )
        if capability == "type" and not text_like:
            return f"blocked type on {label!r} ({kind}): target an editable input control"
        if capability == "select_option" and not choice_like:
            return f"blocked select_option on {label!r} ({kind}): target a choice control"
        if capability == "tap" and choice_like:
            return f"blocked tap on {label!r} ({kind}): use select_option to mutate it"
    for scope in frame.requirement_scopes.values():
        detail = scope.get("detail_resolution")
        detail = detail if isinstance(detail, dict) else {}
        observed = set(detail.get("current_observed_detail_fields") or [])
        required = set(detail.get("detail_fields") or [])
        description = str(args.get("description") or "").casefold()
        if (
            capability == "scroll"
            and required
            and required.issubset(observed)
            and any(str(field).casefold() in description for field in required)
        ):
            return (
                "blocked redundant scroll: enhanced observation already contains every "
                "required detail field; continue from the observed values"
            )
        if scope.get("status") != "unmet" or (
            detail.get("status") == "active"
            and detail.get("pending_candidate_ordinal") is not None
        ):
            continue
        if control is not None and control.get("group_index") is not None:
            return (
                "blocked row action because acquisition scope remains unmet; do not "
                f"claim it is met (requested={scope.get('requested_filters') or {}}, "
                f"applied={scope.get('applied_filters') or {}}). Resolve the filter "
                "blockers before opening a record"
            )
    return ""


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
    """Return a semantic signature tolerant of aliases and coordinate jitter."""
    del tool  # Task action aliases must not bypass the logical-action fuse.
    payload: dict[str, Any] = {
        "capability": capability,
    }
    for field_name in _SIGNATURE_FIELDS:
        value = args.get(field_name)
        if value not in (None, ""):
            payload[field_name] = value
    for coordinate in ("x", "y", "to_x", "to_y"):
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
        "controls": [
            {
                key: control.get(key)
                for key in ("kind", "label", "value", "focused", "checked", "selected")
                if control.get(key) not in (None, "")
            }
            for control in frame.controls
            if any(
                control.get(key) not in (None, "")
                for key in ("value", "focused", "checked", "selected")
            )
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
        compatibility_error = _action_boundary_error(capability, args, frame)
        if compatibility_error:
            return ActionCircuitDecision(
                blocked=True,
                signature=signature,
                progress=progress,
                prior_attempts=prior,
                reason=compatibility_error,
            )
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
