"""Deterministic action guards for one autonomous GUI Worker."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
_AUTH_CODE_MARKER = r"(?:verification code|one-time code|otp|验证码|校验码|动态码)"
_AUTH_CODE_RE = re.compile(
    rf"{_AUTH_CODE_MARKER}\D{{0,32}}(?<!\d)(\d{{4,8}})(?!\d)"
    rf"|(?<!\d)(\d{{4,8}})(?!\d)\D{{0,32}}{_AUTH_CODE_MARKER}",
    re.IGNORECASE,
)


def auth_codes_from_text(text: str) -> set[str]:
    """Extract digits close to an authentication marker, not unrelated numbers."""

    return {value for match in _AUTH_CODE_RE.finditer(text)
            for value in match.groups() if value}


def auth_codes_from_frame(frame: MaterializedFrame) -> set[str]:
    """Return transient authentication codes exposed as current non-input text."""

    codes: set[str] = set()
    for control in frame.controls:
        kind = str(control.get("kind") or "").casefold()
        if not any(token in kind for token in ("input", "textbox", "editor")):
            codes.update(auth_codes_from_text(" ".join(
                str(control.get(key) or "") for key in ("label", "value")
            )))
    return codes


def control_at_point(
    args: dict[str, Any], frame: MaterializedFrame,
) -> dict[str, Any] | None:
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


def is_candidate_commit(
    args: dict[str, Any], frame: MaterializedFrame,
) -> bool:
    """Identify a confirmed selection commit from its pre-action frame."""
    target = control_at_point(args, frame)
    candidates = [
        item for item in frame.controls
        if item.get("in_viewport") is not False
        and item.get("selection_mode") == "multiple"
    ]
    return bool(
        target
        and target.get("form_action") == "commit"
        and any(item.get("is_filter") is True for item in frame.controls)
        and candidates
        and all(bool(item.get("selected")) for item in candidates)
    )


def _action_boundary_error(
    capability: str,
    args: dict[str, Any],
    frame: MaterializedFrame,
    observed_auth_codes: set[str],
) -> str:
    """Reject actions contradicted by authoritative enhanced observation."""

    control = control_at_point(args, frame)
    if control is not None:
        kind = str(control.get("kind") or "").casefold()
        label = str(control.get("label") or kind or "control")
        choice_like = any(
            key in control for key in ("options", "selected_text", "selected_text_primary")
        )
        text_like = any(
            token in kind for token in ("input", "textarea", "textbox", "editor")
        ) or ("combobox" in kind and not choice_like)
        if capability == "type" and not text_like:
            return f"blocked type on {label!r} ({kind}): target an editable input control"
        if capability == "select_option" and not choice_like:
            return f"blocked select_option on {label!r} ({kind}): target a choice control"
        if capability == "tap" and choice_like:
            return f"blocked tap on {label!r} ({kind}): use select_option to mutate it"
    selectable = bool(control and (
        str(control.get("kind") or "").casefold()
        in {"checkbox", "checkbox_input", "radio", "radio_input", "switch", "switch_input"}
        or control.get("selection_mode") in {"single", "multiple"}
    ))
    x, y = args.get("x"), args.get("y")
    if (
        capability in {"tap", "long_press"}
        and not selectable
        and isinstance(x, (int, float))
        and isinstance(y, (int, float))
    ):
        for region in frame.visible_collection_regions:
            for cell in region.get("cells") or ():
                bounds = cell.get("bounds") or ()
                if (
                    len(bounds) == 4
                    and (cell.get("clipped_top") or cell.get("clipped_bottom"))
                    and float(bounds[0]) <= float(x) <= float(bounds[2])
                    and float(bounds[1]) <= float(y) <= float(bounds[3])
                ):
                    return (
                        "spatial target lies inside a clipped collection cell; scroll it "
                        "into the unobscured central viewport before acting"
                    )
    description = str(args.get("description") or "").casefold()
    auth_context = " ".join((
        description,
        str((control or {}).get("label") or "").casefold(),
    ))
    text = str(args.get("text") or "").strip()
    if (
        capability == "type"
        and re.fullmatch(r"\d{4,8}", text)
        and re.search(_AUTH_CODE_MARKER, auth_context, re.IGNORECASE)
        and text not in observed_auth_codes
    ):
        return (
            "blocked unobserved authentication code: open its delivery surface, "
            "read the exact current code, then return and enter it"
        )
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


def _coordinate_bucket(value: Any, *, size: int = 50) -> int | None:
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
        if capability == "scroll" and field_name == "amount":
            continue
        value = args.get(field_name)
        if value not in (None, ""):
            payload[field_name] = value
    if capability != "scroll":
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
        "visible_collections": frame.visible_collection_regions,
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
    """Block repeated actions and strict two-state action cycles."""

    threshold: int = 2
    _last_attempt: tuple[str, str] | None = None
    _consecutive_attempts: int = 0
    _recent_attempts: tuple[tuple[str, str], ...] = ()

    def inspect(
        self,
        *,
        tool: str,
        capability: str,
        args: dict[str, Any],
        frame: MaterializedFrame,
        observed_auth_codes: set[str] | None = None,
    ) -> ActionCircuitDecision:
        signature = action_signature(tool=tool, capability=capability, args=args)
        progress = progress_signature(frame)
        attempt = (signature, progress)
        history = self._recent_attempts
        prior = self._consecutive_attempts if attempt == self._last_attempt else 0
        compatibility_error = _action_boundary_error(
            capability, args, frame, observed_auth_codes or set()
        )
        if compatibility_error:
            return ActionCircuitDecision(
                blocked=True,
                signature=signature,
                progress=progress,
                prior_attempts=prior,
                reason=compatibility_error,
            )
        guarded = capability in _GUARDED_CAPABILITIES
        cycle = (
            len(history) >= 2
            and history[-2] == attempt != history[-1]
            and (len(history) < 3 or history[-3] != attempt)
        )
        blocked = guarded and (prior >= self.threshold or cycle)
        reason = ""
        if blocked:
            reason = (
                "blocked two-state action cycle without task-relevant progress"
                if cycle else
                f"blocked repeated {capability} action after {prior} equivalent "
                "dispatches without task-relevant progress"
            )
        return ActionCircuitDecision(
            blocked=blocked,
            signature=signature,
            progress=progress,
            prior_attempts=2 if cycle else prior,
            reason=reason,
        )

    def record(self, decision: ActionCircuitDecision) -> None:
        attempt = (decision.signature, decision.progress)
        self._recent_attempts = (*self._recent_attempts[-3:], attempt)
        self._consecutive_attempts = (
            self._consecutive_attempts + 1 if attempt == self._last_attempt else 1
        )
        self._last_attempt = attempt

    def reset(self) -> None:
        self._last_attempt = None
        self._consecutive_attempts = 0
        self._recent_attempts = ()


__all__ = [
    "ActionCircuitDecision",
    "WorkerActionCircuitBreaker",
    "action_signature",
    "auth_codes_from_frame",
    "auth_codes_from_text",
    "is_candidate_commit",
    "control_at_point",
    "progress_signature",
]
