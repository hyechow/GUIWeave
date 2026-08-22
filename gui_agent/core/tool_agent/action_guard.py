"""Deterministic action guards for one autonomous GUI Worker."""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from gui_agent.core.tool_agent.contracts import MaterializedFrame
from gui_agent.core.tool_agent.action_geometry import control_at_point


_AUTH_CODE_MARKER = r"(?:verification code|one-time code|otp|验证码|校验码|动态码)"
_AUTH_CODE_RE = re.compile(
    rf"{_AUTH_CODE_MARKER}\D{{0,32}}(?<!\d)(\d{{4,8}})(?!\d)"
    rf"|(?<!\d)(\d{{4,8}})(?!\d)\D{{0,32}}{_AUTH_CODE_MARKER}",
    re.IGNORECASE,
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


def action_boundary_error(
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
    return ""
