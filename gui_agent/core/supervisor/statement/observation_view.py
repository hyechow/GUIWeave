"""One-frame affordance projection for Statement Transition.

The projection exposes adapter mechanics (where an action can land and which primitive
operations that target supports).  It never decides task progress, completion, or route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urldefrag

from gui_agent.core.schemas import Observation, PolicyTurn, StatementContract


TransitionOperation = Literal[
    "activate",
    "input",
    "select",
    "navigate",
    "iterate",
]
AffordanceCoverage = Literal["unavailable", "unknown", "partial", "complete"]


def _semantic_label(node: dict) -> str:
    """Readable affordance label, falling back to the structural resource id.

    Icon controls carry a private-use glyph (e.g. a Material Design Icon char) or an
    empty key, which is meaningless to the Transition LLM — it cannot tell the
    channel-list "+" from the server icon, so it estimates a point blindly. The
    resource id (`channel_list_header.plus.button`) is the one place that semantic
    lives; surface its trailing meaningful segment as the label so the supervisor can
    bind the exact ref instead of guessing. A readable key/label always wins.
    """
    key = str(node.get("key") or node.get("label") or "").strip()
    if key and any(char.isalnum() or "一" <= char <= "鿿" for char in key):
        return key
    resource = str(node.get("resource") or "").strip()
    if resource:
        segments = [seg for seg in resource.split(".") if seg and not seg.isdigit()]
        for seg in reversed(segments):
            if seg not in {"button", "icon", "image", "text", "input", "view"}:
                return seg
        if segments:
            return segments[-1]
    return key

_ACTIVATABLE_ROLES = frozenset({
    "button",
    "checkbox",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "radio",
    "switch",
    "tab",
    "treeitem",
})
_INPUT_ROLES = frozenset({"textbox", "searchbox", "input"})
_SELECT_ROLES = frozenset({"combobox", "listbox", "option"})
_SELECT_CONTROL_KINDS = frozenset({
    "select",
    "native_select",
    "selectmenu",
    "combobox",
    "listbox",
    "option",
})


@dataclass(frozen=True)
class StatementObservationView:
    """Visual-first current-frame evidence consumed by Transition and validation.

    The screenshot is always the base observation.  Adapter semantic sensors may add
    positive target identity/capability facts, but their absence never means that the
    screenshot contains no actionable target.
    """

    affordance_coverage: AffordanceCoverage
    affordances: tuple[dict, ...]


def _affordance_coverage(observation: Observation) -> AffordanceCoverage:
    # The complete control-state index is evidence about values, not about the
    # target list projected below.  Coverage here therefore follows only the
    # affordance-producing sensors.
    meta = observation.form_controls_meta or {}
    coverage = str(meta.get("coverage") or "").strip().casefold()
    if coverage == "partial":
        return "partial"
    if coverage == "complete":
        return "complete"
    sensor_available = (
        observation.semantic_tree is not None
        or observation.form_controls is not None
        or observation.form_controls_meta is not None
    )
    return "unknown" if sensor_available else "unavailable"


def _usable_navigation_url(url: str, current_url: str) -> bool:
    value = url.strip()
    if not value.startswith(("http://", "https://")):
        return False
    target, fragment = urldefrag(value)
    current, _ = urldefrag(current_url or "")
    same_document = target.rstrip("/") == current.rstrip("/")
    if not target or same_document and not fragment:
        return False
    if same_document and fragment in {"", "#"}:
        return False
    return True


def _visibility(item: dict) -> str:
    if item.get("in_viewport") is True:
        return "visible"
    if item.get("in_viewport") is False:
        return "offscreen"
    return "unknown"


def _semantic_affordance(node: dict, current_url: str) -> dict | None:
    role = str(node.get("role") or "").strip().casefold()
    label = _semantic_label(node)
    if not role or not label:
        return None
    visibility = _visibility(node)
    url = str(node.get("url") or node.get("href") or "").strip()
    operations: list[TransitionOperation] = []
    row_activation = role == "row" and label.startswith(("http://", "https://"))
    actionable_role = (
        role in _ACTIVATABLE_ROLES | _INPUT_ROLES | _SELECT_ROLES
        or row_activation
    )
    if visibility == "offscreen" and actionable_role:
        operations.append("iterate")
    elif visibility == "visible":
        if role in _ACTIVATABLE_ROLES or row_activation:
            operations.append("activate")
        if role == "link" and _usable_navigation_url(url, current_url):
            operations.append("navigate")
        if role in _INPUT_ROLES:
            operations.append("input")
        if role in _SELECT_ROLES:
            operations.append("select")
        # A text/combobox field is focused or opened by tapping it (activate),
        # then its value is set — same rule as _control_affordance.
        if role in _INPUT_ROLES | _SELECT_ROLES:
            operations.append("activate")
    if not operations:
        return None
    result = {
        "label": label,
        "ref": str(node.get("ref") or "").strip(),
        "role": role,
        "visibility": visibility,
        "supported_operations": operations,
    }
    if url:
        result["url"] = url
    for key in ("query_action", "selected", "value"):
        value = node.get(key)
        if value is not None and value != "":
            result[key] = value
    point = node.get("point")
    if isinstance(point, dict):
        result["point"] = point
    return result


def _control_affordance(control: dict) -> dict | None:
    kind = str(control.get("kind") or control.get("type") or "").strip().casefold()
    label = str(
        control.get("label")
        or control.get("name")
        or control.get("id")
        or ""
    ).strip()
    if not kind or not label:
        return None
    visibility = _visibility(control)
    if visibility == "offscreen":
        operations: list[TransitionOperation] = ["iterate"]
    elif kind in _SELECT_CONTROL_KINDS:
        # A select/combobox/date-field opens by tapping it, then an option is
        # chosen; both operations are legitimate on the same control.
        operations = ["select", "activate"]
    elif kind in {
        "input",
        "text_input",
        "textarea",
        "search",
        "searchbox",
        "textbox",
        "number",
    }:
        # A text field is focused by tapping it (activate), then typed into (input).
        operations = ["input", "activate"]
    elif kind in {
        "button", "checkbox", "checkbox_input", "radio", "radio_input",
        "switch", "switch_input", "link", "section_toggle",
    }:
        operations = ["activate"]
    else:
        return None
    refs = [
        value
        for value in dict.fromkeys(
            str(control.get(field) or "").strip()
            for field in ("ref", "id", "name")
        )
        if value
    ]
    result = {
        "label": label,
        "ref": refs[0] if refs else "",
        "role": kind,
        "visibility": visibility,
        "supported_operations": operations,
    }
    if len(refs) > 1:
        result["ref_aliases"] = refs[1:]
    for key in (
        "name", "id", "value", "options", "group_id", "group_index",
        "group_field", "is_filter", "query_action", "form_action",
    ):
        value = control.get(key)
        if value is not None and value != "":
            result[key] = value
    return result


def _role_specificity(role: str) -> int:
    # Form-control roles carry the semantic kind (input/select/textarea); the raw
    # accessibility-tree roles (textbox/button) are generic. Prefer the specific one.
    return 1 if role in {
        "input", "select", "textarea", "number", "search", "checkbox", "radio",
        "switch", "link", "section_toggle",
    } else 0


def build_observation_view(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
) -> StatementObservationView:
    """Expose optional semantic targets without judging visual or Statement state."""
    del statement, history

    raw_affordances: list[dict] = []
    for raw in observation.semantic_tree or []:
        item = _semantic_affordance(raw, str(observation.url or "")) if isinstance(raw, dict) else None
        if item is not None:
            raw_affordances.append(item)
    controls = [
        *(observation.form_control_state or []),
        *(observation.form_controls or []),
    ]
    for raw in controls:
        item = _control_affordance(raw) if isinstance(raw, dict) else None
        if item is not None:
            raw_affordances.append(item)

    # Dedupe by node identity (ref): the same UIAutomator node is surfaced by both the
    # semantic tree and the form-control index with different labels/roles, which would
    # otherwise make target_ref resolution ambiguous. Keep the more specific role at the
    # first-seen position, preserving the original ordering.
    affordances: list[dict] = []
    ref_index: dict[str, int] = {}
    for item in raw_affordances:
        ref = str(item.get("ref") or "")
        if not ref:
            affordances.append(item)
            continue
        existing_index = ref_index.get(ref)
        if existing_index is None:
            ref_index[ref] = len(affordances)
            affordances.append(item)
        elif _role_specificity(str(item.get("role") or "")) > _role_specificity(str(affordances[existing_index].get("role") or "")):
            affordances[existing_index] = item

    return StatementObservationView(
        affordance_coverage=_affordance_coverage(observation),
        affordances=tuple(affordances),
    )


__all__ = [
    "StatementObservationView",
    "AffordanceCoverage",
    "TransitionOperation",
    "build_observation_view",
]
