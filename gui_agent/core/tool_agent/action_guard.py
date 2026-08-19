"""Deterministic action guards for one autonomous GUI Worker."""

from __future__ import annotations

import json
import ipaddress
import re
import socket
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import urlsplit

from gui_agent.core.tool_agent.contracts import (
    CollectionRef,
    DynamicActionSpec,
    MaterializedFrame,
    WorkerSpec,
    positioned_rect,
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


@dataclass(frozen=True)
class NavigationAdmission:
    decision: Literal["allow", "abort"]
    reason: str = ""


@dataclass(frozen=True)
class FrameAssessment:
    allowed_actions: list[DynamicActionSpec]
    ready_collection: CollectionRef | None = None
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


def ready_collection(spec: WorkerSpec, frame: MaterializedFrame) -> CollectionRef | None:
    if spec.profile != "collector" or not spec.data_requirements:
        return None
    requirement_id = spec.data_requirements[0].id
    cands = [item for item in (frame.collections or [])
             if item.requirement_id == requirement_id]
    if not cands:
        return None
    # Pure ReAct collector: any collection item for the requirement is usable.
    # The LLM decides completion; runtime snapshots accumulated or current rows.
    return cands[-1]


def _approach_order_is_applied(spec: WorkerSpec, frame: MaterializedFrame) -> bool:
    """Whether a sort named by the approach is reflected in current control state."""

    approach = spec.strategy.approach.casefold()
    if "sort" not in approach:
        return True
    contract = " ".join([*spec.success_criteria, spec.strategy.approach]).casefold()
    control = next((
        item for item in frame.controls
        if "sort" in str(item.get("label") or "").casefold()
    ), None)
    if control is None:
        return False
    selected = str(
        control.get("selected_text_primary")
        or control.get("selected_text")
        or control.get("value")
        or ""
    ).casefold()
    requested = ""
    for value in control.get("options") or ():
        normalized = str(value).strip().casefold()
        option = re.escape(normalized)
        if normalized and re.search(
            rf"\b(?:sort(?:ed)?\s+by\s+{option}|"
            rf"(?:{option}\s+(?:ascending|descending)|"
            rf"(?:ascending|descending)\s+{option})\s+(?:sort|order))\b",
            contract,
        ):
            requested = normalized
            break
    if not requested or selected != requested:
        return False
    return not _requested_sort_direction_is_pending(spec, frame)


def _requested_sort_direction_is_pending(
    spec: WorkerSpec, frame: MaterializedFrame,
) -> bool:
    """Whether a visible action offers the sort direction still requested."""

    contract = " ".join([
        spec.goal, *spec.success_criteria, spec.strategy.approach,
    ]).casefold()
    if not re.search(r"\b(?:sort|order|alphabetic)", contract):
        return False
    if re.search(r"\b(?:descending|reverse\s+alphabetical(?:ly)?)\b", contract):
        direction = "descending"
    elif re.search(r"\b(?:ascending|alphabetical(?:ly)?)\b", contract):
        direction = "ascending"
    else:
        return False
    return any(
        re.search(
            rf"\b(?:set|change|switch)(?:\s+direction)?(?:\s+to)?\s+{direction}\b",
            str(control.get("label") or ""),
            re.I,
        )
        for control in frame.controls
    )


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
    collection = ready_collection(spec, frame)
    requirement = spec.data_requirements[0] if spec.data_requirements else None
    boundary = requirement if requirement and (
        requirement.coverage == "first_match" and requirement.cardinality == "many"
    ) else None
    order_applied = boundary is None or _approach_order_is_applied(spec, frame)
    boundary_ready = bool(
        boundary
        and order_applied
        and collection is not None
        and collection.coverage.get("status") == "complete"
        and any(
            chunk.requirement_id == boundary.id
            and chunk.frame_id == frame.frame_id
            and chunk.row_count > 0
            and chunk.coverage.get("start_visible") is True
            for chunk in frame.chunks
        )
    )
    if boundary and order_applied:
        actions = [action for action in actions if action.capability != "reveal_control"]
    if boundary_ready:
        actions = []
    if spec.profile == "operator":
        mode: Literal["unavailable", "operator", "collector"] = (
            "unavailable"
            if _requested_sort_direction_is_pending(spec, frame)
            else "operator"
        )
    elif spec.profile == "collector":
        mode = (
            "unavailable"
            if boundary and not boundary_ready
            else "collector"
        )
    else:
        mode = "unavailable"
    return FrameAssessment(actions, collection, mode)


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


def _is_scope_control(control: dict[str, Any] | None) -> bool:
    """True when the target can change applied filters."""

    if control is None:
        return False
    if control.get("is_filter") is True:
        return True
    if control.get("form_action") in {"commit", "reset", "clear", "query"}:
        return True
    return control.get("query_action") not in (None, "")


def control_at_point(
    args: dict[str, Any], frame: MaterializedFrame,
) -> dict[str, Any] | None:
    """Return the smallest visible enhanced control containing the action point."""

    x, y = args.get("x"), args.get("y")
    if not all(isinstance(value, (int, float)) for value in (x, y)):
        return None
    matches: list[tuple[float, dict[str, Any]]] = []
    for control in frame.controls:
        if control.get("in_viewport") is False:
            continue
        rect = positioned_rect(control)
        if rect is None:
            continue
        cx, cy = float(rect["x"]), float(rect["y"])
        width, height = float(rect.get("w") or 0), float(rect.get("h") or 0)
        if abs(float(x) - cx) <= width / 2 and abs(float(y) - cy) <= height / 2:
            matches.append((max(1.0, width * height), control))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def ordered_boundary_resume_feedback(
    spec: WorkerSpec,
    frame: MaterializedFrame,
    memory: str,
) -> dict[str, Any] | None:
    """Name the next same-row record after a boundary inspection Back."""

    contract = " ".join((spec.goal, *spec.success_criteria, spec.strategy.approach))
    tools = re.findall(r"\btool=([a-z_]+)\b", memory)
    last_back = max((index for index, tool in enumerate(tools) if tool == "back"), default=-1)
    if not (
        re.search(
            r"\b(?:cheapest|lowest|highest|earliest|latest|oldest|newest|"
            r"minimum|maximum|least expensive|most expensive)\b",
            contract,
            re.I,
        )
        and re.search(r"\b(?:ascending|descending|sort|order)\b", contract, re.I)
        and last_back >= 0
        and all(tool in {"scroll", "reveal_control"} for tool in tools[last_back + 1:])
    ):
        return None
    anchors = [
        (control, rect) for control in frame.controls
        if (rect := positioned_rect(control)) is not None
        if control.get("kind") == "a"
        and control.get("in_viewport") is not False
        and str(control.get("label") or "").strip()
    ]
    inspected_memory = memory[:memory.rfind("tool=back")]
    seen = [
        (inspected_memory.rfind(str(control["label"]).rstrip("…").strip()), control, rect)
        for control, rect in anchors
    ]
    opened = max((item for item in seen if item[0] >= 0), default=None)
    if opened is None:
        return None
    rect = opened[2]
    successors = [
        (control, candidate_rect) for control, candidate_rect in anchors
        if abs(float(candidate_rect["y"]) - float(rect["y"])) <= 2
        and float(candidate_rect["x"]) > float(rect["x"])
    ]
    successor = min(
        successors,
        key=lambda item: float(item[1]["x"]),
        default=None,
    )
    if successor is None:
        return None
    control, rect = successor
    return {
        "status": "ordered_boundary_resume",
        "next_record": control.get("label"),
        "next_record_rect": rect,
        "instruction": (
            "The preceding record was rejected. Open this exact immediate next "
            "record at its Runtime rect center; do not reopen or skip."
        ),
    }


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
        if capability not in {"tap", "click"} or _is_scope_control(control):
            continue
        return (
            "blocked action because acquisition scope remains unmet; do not "
            f"claim it is met (requested={scope.get('requested_filters') or {}}, "
            f"applied={scope.get('applied_filters') or {}}). Resolve the filter "
            "blockers before collecting"
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
            (item.requirement_id, item.row_count, item.coverage.get("status"),
             item.coverage.get("pages_seen"))
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


_CYCLE_WINDOW = 6
# A traversal loop revisiting at most this many distinct surfaces inside the
# window is stuck: progressing flows (form fill, collection scroll, detail
# walk) mint a new progress hash per dispatch. Half the window keeps the fuse
# tolerant of one legitimate back-and-forth (review a step, then continue).
_CYCLE_MAX_DISTINCT = 3


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
    """Block an identical action when task-relevant progress is unchanged."""

    _last_attempt: tuple[str, str] | None = None
    _progress_window: tuple[str, ...] = ()

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
        repeated = attempt == self._last_attempt
        compatibility_error = _action_boundary_error(
            capability, args, frame, observed_auth_codes or set()
        )
        if compatibility_error:
            return ActionCircuitDecision(
                blocked=True,
                signature=signature,
                progress=progress,
                prior_attempts=int(repeated),
                reason=compatibility_error,
                instruction=_DEFAULT_BLOCK_INSTRUCTION,
            )
        # A coordinate/description jitter lets a Next/Back traversal loop dodge the
        # repeat check above. The robust stuck signal is surface-level: a worker
        # whose last N dispatches only revisited already-seen surfaces is cycling,
        # regardless of which action moved it there. Progressing flows (form fill,
        # collection scroll, detail walk) keep minting new progress hashes and never
        # trip this fuse. record() trims the window to _CYCLE_WINDOW entries.
        window = self._progress_window
        surface_cycle = (
            len(window) == _CYCLE_WINDOW
            and len(set(window)) <= _CYCLE_MAX_DISTINCT
        )
        blocked = capability in _GUARDED_CAPABILITIES and (repeated or surface_cycle)
        reason = ""
        if blocked:
            reason = (
                f"blocked repeated {capability} action without task-relevant progress"
                if repeated
                else "blocked surface cycle: recent dispatches only revisited "
                "already-seen surfaces without new task-relevant state; stop the "
                "traversal loop and change the pending state (perform the untried "
                "selection/mutation) or fail with the concrete blocker"
            )
        return ActionCircuitDecision(
            blocked=blocked,
            signature=signature,
            progress=progress,
            prior_attempts=int(repeated),
            reason=reason,
            instruction=_DEFAULT_BLOCK_INSTRUCTION if blocked else "",
        )

    def record(self, decision: ActionCircuitDecision) -> None:
        self._last_attempt = (decision.signature, decision.progress)
        # One surface visit per entry: a multi-action batch decided on a single
        # frame (login = type, type, tap) repeats that frame's hash per atomic
        # dispatch, but it is one decision, not a traversal loop.
        if not self._progress_window or self._progress_window[-1] != decision.progress:
            self._progress_window = (
                *self._progress_window[-_CYCLE_WINDOW + 1:], decision.progress,
            )

    def reset(self) -> None:
        self._last_attempt = None
        self._progress_window = ()
