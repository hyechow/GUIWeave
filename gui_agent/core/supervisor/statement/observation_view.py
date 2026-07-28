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
    label = str(node.get("key") or node.get("label") or "").strip()
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
    for key in ("query_action",):
        value = node.get(key)
        if value:
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
        operations = ["select"]
    elif kind in {
        "input",
        "text_input",
        "textarea",
        "search",
        "searchbox",
        "textbox",
        "number",
    }:
        operations = ["input"]
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


def build_observation_view(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
) -> StatementObservationView:
    """Expose optional semantic targets without judging visual or Statement state."""
    del statement, history

    affordances: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in observation.semantic_tree or []:
        item = _semantic_affordance(raw, str(observation.url or "")) if isinstance(raw, dict) else None
        if item is None:
            continue
        key = (item["label"].casefold(), item["ref"], item["role"])
        if key not in seen:
            seen.add(key)
            affordances.append(item)
    controls = [
        *(observation.form_control_state or []),
        *(observation.form_controls or []),
    ]
    for raw in controls:
        item = _control_affordance(raw) if isinstance(raw, dict) else None
        if item is None:
            continue
        key = (item["label"].casefold(), item["ref"], item["role"])
        if key not in seen:
            seen.add(key)
            affordances.append(item)

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
