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

_ACTIVATABLE_ROLES = frozenset({
    "button",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "treeitem",
})
_INPUT_ROLES = frozenset({"textbox", "searchbox", "input"})
_SELECT_ROLES = frozenset({"combobox", "listbox", "option"})


@dataclass(frozen=True)
class StatementObservationView:
    """Current-frame mechanical facts consumed by Transition and dispatch validation."""

    control_coverage: str
    affordances: tuple[dict, ...]


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
    elif kind in {"select", "combobox", "listbox", "option"}:
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
    elif kind in {"button", "checkbox", "radio", "switch", "link"}:
        operations = ["activate"]
    else:
        return None
    result = {
        "label": label,
        "ref": str(
            control.get("ref") or control.get("name") or control.get("id") or ""
        ).strip(),
        "role": kind,
        "visibility": visibility,
        "supported_operations": operations,
    }
    for key in ("name", "id", "value", "options", "group_id"):
        value = control.get(key)
        if value is not None and value != "":
            result[key] = value
    return result


def build_observation_view(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
) -> StatementObservationView:
    """Expose current targets and capabilities without judging Statement state."""
    del statement, history
    meta = (
        observation.form_control_state_meta
        if observation.form_control_state is not None
        else observation.form_controls_meta
    ) or {}
    control_coverage = str(meta.get("coverage") or "unknown")

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
    for raw in observation.form_controls or []:
        item = _control_affordance(raw) if isinstance(raw, dict) else None
        if item is None:
            continue
        key = (item["label"].casefold(), item["ref"], item["role"])
        if key not in seen:
            seen.add(key)
            affordances.append(item)

    return StatementObservationView(
        control_coverage=control_coverage,
        affordances=tuple(affordances),
    )


__all__ = [
    "StatementObservationView",
    "TransitionOperation",
    "build_observation_view",
]
