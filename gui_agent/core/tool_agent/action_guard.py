"""Deterministic action guards for one autonomous GUI Worker."""

from __future__ import annotations

import json
import ipaddress
import re
import socket
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import urlsplit

from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerSpec,
)


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
# Recent (signature, progress) attempts retained for repeat detection. Sized to
# exceed a typical multi-step GUI cycle (select → menu → cancel → re-select ≈ 6)
# so a repeated step lands inside the window before the cycle closes again.
_ATTEMPT_WINDOW = 8
# Entries kept after a progress reset() so a loop containing a commit/scroll step
# still registers its repetition on the next cycle.
_RESET_RETAIN = 2
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
_DEFAULT_BLOCK_INSTRUCTION = (
    "Treat the Runtime guard as authoritative. Do not retry the blocked action; "
    "advance from the current observation or choose a materially different capability."
)
# A Worker hallucination: `{{new_name}}` (or `{new_name}`) typed literally because
# the model could not see the Runtime-injected binding value. It is never a real
# input value — binding actions inject actual values and a generic `type` must fill
# a literal visible string. Blocking here turns a silent phantom-file mutation into
# a correctable action rejection.
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"\{\{?\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}?\}"
)


@dataclass(frozen=True)
class NavigationAdmission:
    decision: Literal["allow", "abort"]
    reason: str = ""


@dataclass(frozen=True)
class FrameAssessment:
    allowed_actions: list[DynamicActionSpec]
    completion_mode: Literal["unavailable", "operator", "collector"] = "unavailable"


def _http_origin(value: str, *, require_public: bool = False) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        scheme, host, port = parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    if require_public:
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            try:
                address = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
            except OSError:
                address = None
        if (
            host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
            or address is not None and not address.is_global
        ):
            return None
    default_port = 443 if scheme == "https" else 80
    return scheme, host, None if port == default_port else port


def assess_navigation_url(
    candidate: str,
) -> NavigationAdmission:
    """Validate only the transport and network-safety boundary of a URL."""

    candidate = candidate.strip()
    origin = _http_origin(candidate)
    if origin is None:
        return NavigationAdmission(
            "abort", "navigation requires an absolute HTTP(S) URL without credentials"
        )
    if _http_origin(candidate, require_public=True) is None:
        return NavigationAdmission(
            "abort", "navigation to private, loopback, link-local, or reserved destinations is denied"
        )
    return NavigationAdmission("allow")


def assess_frame(
    spec: WorkerSpec,
    actions: list[DynamicActionSpec],
    frame: MaterializedFrame,
    *,
    attempted_action: bool = False,
) -> FrameAssessment:
    if frame.readiness != "ready":
        allowed = [] if attempted_action else [
            action for action in actions
            if action.capability in {"open_url", "back", "home", "app_switch", "launch_app"}
        ]
        return FrameAssessment(
            allowed,
            completion_mode="unavailable",
        )
    # Collection completeness is a Worker-judged claim: the collector decides from
    # its own evidence when the scope is fully acquired. Runtime never certifies it,
    # so `complete` is offered unconditionally for either profile on a ready frame.
    mode: Literal["unavailable", "operator", "collector"] = (
        "operator" if spec.profile == "operator" else "collector"
    )
    return FrameAssessment(actions, completion_mode=mode)


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
        if control.get("enabled") is False:
            return f"blocked action on disabled control {label!r}"
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
    x, y = args.get("x"), args.get("y")
    if (
        capability in {"tap", "long_press"}
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
                    cell_ref = str(cell.get("ref") or "")
                    control_ref = str((control or {}).get("ref") or "")
                    if control_ref and cell_ref and not (
                        control_ref == cell_ref
                        or control_ref.startswith(cell_ref + ".")
                    ):
                        continue
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
    if capability == "type" and _TEMPLATE_PLACEHOLDER_RE.search(text):
        return (
            "blocked type: text contains a template placeholder like '{{new_name}}', "
            "which is not a real value; use the Runtime binding action for this input "
            "(e.g. new_name) or type the exact visible value"
        )
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
    control_fields = ("kind", "label", "value", "focused", "checked", "selected")
    payload = {
        "page": (frame.url, frame.title),
        "scopes": {
            key: (value.get("status"), value.get("applied_filters"))
            for key, value in sorted(frame.requirement_scopes.items())
        },
        "collections": [
            (item.requirement_id, item.row_count)
            for item in frame.collections
        ],
        "visible": frame.visible_collection_regions,
        "controls": [
            tuple(control.get(key) for key in control_fields)
            for control in frame.controls
            if any(control.get(key) not in (None, "") for key in control_fields[2:])
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
    instruction: str = ""


@dataclass
class WorkerActionCircuitBreaker:
    """Block an action already executed in an identical task-relevant state.

    A single-slot memory cannot see multi-step GUI loops (select → open menu →
    cancel → re-select): intervening actions overwrite it before the cycle
    closes, so each repeated step looks novel. Keep a bounded window of recent
    (signature, progress) attempts and block when the same pair recurs inside
    it; the window is sized to cover a full GUI cycle.
    """

    _attempts: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=_ATTEMPT_WINDOW)
    )

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
        prior_attempts = sum(1 for item in self._attempts if item == attempt)
        compatibility_error = _action_boundary_error(
            capability, args, frame, observed_auth_codes or set()
        )
        if compatibility_error:
            return ActionCircuitDecision(
                blocked=True,
                signature=signature,
                progress=progress,
                prior_attempts=prior_attempts,
                reason=compatibility_error,
                instruction=_DEFAULT_BLOCK_INSTRUCTION,
            )
        blocked = capability in _GUARDED_CAPABILITIES and prior_attempts > 0
        reason = (
            f"blocked repeated {capability} action without task-relevant progress"
            if blocked else ""
        )
        return ActionCircuitDecision(
            blocked=blocked,
            signature=signature,
            progress=progress,
            prior_attempts=prior_attempts,
            reason=reason,
            instruction=_DEFAULT_BLOCK_INSTRUCTION if blocked else "",
        )

    def record(self, decision: ActionCircuitDecision) -> None:
        self._attempts.append((decision.signature, decision.progress))

    def reset(self, trigger_signature: str | None = None) -> None:
        # A "progress" event (candidate commit / effective scroll) releases the
        # fuse for the action that produced it, but clearing the whole window
        # would erase the evidence of a loop that happens to contain such a step
        # (select → commit → undo → re-select). Release only the triggering
        # action's entries and keep the rest so the next cycle still registers.
        if trigger_signature is None:
            self._attempts.clear()
            return
        retained = [
            item for item in self._attempts if item[0] != trigger_signature
        ]
        self._attempts = deque(retained[-_RESET_RETAIN:], maxlen=_ATTEMPT_WINDOW)
