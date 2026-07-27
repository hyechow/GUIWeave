"""Optional structural target binding for browser observations."""

from __future__ import annotations

from urllib.parse import urlsplit

from gui_agent.core.schemas import (
    BaseActionDecision,
    Observation,
    SupervisorStep,
    TargetBinding,
    TargetValue,
    target_value_options,
)
from .control_grounding import matches_target_control


_POINT_TARGET_ACTIONS = {"tap", "click", "type", "select_option", "upload"}


def _action_point(action: object) -> tuple[float, float] | None:
    snap = getattr(action, "snap", None)
    snapped = snap.get("snapped") if isinstance(snap, dict) else None
    if isinstance(snapped, (list, tuple)) and len(snapped) == 2:
        x, y = snapped
    else:
        x = getattr(action, "x", None)
        y = getattr(action, "y", None)
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return float(x), float(y)


def _point_matches(control: dict, action: object) -> bool:
    rect = control.get("rect") if isinstance(control.get("rect"), dict) else {}
    point = _action_point(action)
    if (
        point is None
        or not isinstance(rect.get("x"), (int, float))
        or not isinstance(rect.get("y"), (int, float))
    ):
        return False
    half_width = max(3.0, float(rect.get("w") or 0) / 2)
    half_height = max(3.0, float(rect.get("h") or 0) / 2)
    return (
        abs(float(rect["x"]) - point[0]) <= half_width
        and abs(float(rect["y"]) - point[1]) <= half_height
    )


_SELECT_CONTROL_KINDS = {"native_select", "select", "listbox", "combobox"}


def _is_select_control(control: dict) -> bool:
    return str(control.get("kind") or "").strip() in _SELECT_CONTROL_KINDS


def _select_has_option(control: dict, target_value: str) -> bool:
    """Whether a select control's option list contains the declared target value.

    A native ``<select>``'s option list is authoritative identity — unlike its closed-state
    label, which the adapter often fails to extract (a sibling notice/banner id leaks in, the
    visible header text is missed, …). So a unique owning select that carries the target
    option binds deterministically, independent of the label or of mutation authorization.
    Match on the shared semantic key so whitespace / case differences do not defeat it.
    """
    options = control.get("options")
    if not isinstance(options, list) or not target_value:
        return False
    wanted = _semantic_key(target_value)
    return any(
        _semantic_key(option) == wanted
        for option in options
        if isinstance(option, (str, int, float))
    )


def _binding(control: dict) -> TargetBinding:
    group_id = str(control.get("group_id") or "").strip()
    return TargetBinding(
        status="bound",
        source="structural",
        unit_id=group_id or "__form__",
        reason="browser control inventory uniquely owns the concrete action point",
    )


class BrowserTargetBinder:
    """Compare a visual proposal with rendered control ownership.

    Control identity is resolved from the adapter control inventory (a control's own
    label / name / options / group) and the concrete action point. Text identity may
    confirm a binding, but only an exact target-ref point mismatch can contradict it.
    """

    def bind(
        self,
        step: SupervisorStep,
        observation: Observation,
        action_decision: BaseActionDecision,
    ) -> TargetBinding | None:
        intent = step.action_intent
        if intent is None:
            return None
        if intent.target_ref:
            semantic = [
                node
                for node in observation.semantic_tree or []
                if isinstance(node, dict)
                and str(node.get("ref") or "").strip() == intent.target_ref
            ]
            if len(semantic) > 1:
                return TargetBinding(
                    status="unresolved",
                    source="structural",
                    unit_id=f"ref:{intent.target_ref}",
                    reason="declared target_ref is no longer unique in the current frame",
                )
            if len(semantic) == 1:
                action = action_decision.action
                if (
                    getattr(action, "action_type", "") == "navigate"
                    and str(semantic[0].get("url") or "").strip()
                    == str(getattr(action, "url", "") or "").strip()
                ):
                    return TargetBinding(
                        status="bound",
                        source="structural",
                        unit_id=f"ref:{intent.target_ref}",
                        reason="navigation URL is owned by the declared semantic target ref",
                    )
                point = semantic[0].get("point")
                if isinstance(point, dict) and all(
                    isinstance(point.get(axis), (int, float)) for axis in ("x", "y")
                ):
                    action_type = str(getattr(action, "action_type", "") or "")
                    action_point = _action_point(action)
                    if action_type in _POINT_TARGET_ACTIONS and action_point is not None:
                        if (
                            abs(float(point["x"]) - action_point[0]) > 3
                            or abs(float(point["y"]) - action_point[1]) > 3
                        ):
                            return TargetBinding(
                                status="contradicted",
                                source="structural",
                                unit_id=f"ref:{intent.target_ref}",
                                reason="action point does not belong to the declared semantic target ref",
                            )
                        return TargetBinding(
                            status="bound",
                            source="structural",
                            unit_id=f"ref:{intent.target_ref}",
                            reason="action point is owned by the declared semantic target ref",
                        )
            # A ref may come from the optional form-control inventory rather than the
            # semantic tree.  Absence from that namespace is not a contradiction; continue
            # with point ownership and label binding below.
        controls = getattr(observation, "form_controls", None)
        if not controls:
            return None
        semantic = [
            item
            for item in controls
            if isinstance(item, dict)
            and matches_target_control(
                item,
                intent.target_control,
                allow_compound=intent.family in {"input", "select"},
            )
        ]
        point_owners = [
            item
            for item in controls
            if isinstance(item, dict) and _point_matches(item, action_decision.action)
        ]
        candidates = [item for item in semantic if item in point_owners]
        if len(candidates) == 1:
            return _binding(candidates[0])

        # Deterministic select binding: a unique <select> owning the action point and
        # carrying the declared target option IS the target control. The closed-select label
        # is unreliable, but the option list is authoritative — so this binds filter and
        # mutation selects without depending on mutation authorization or a perfect label.
        target_value = intent.target_value.strip()
        if target_value:
            select_owners = [item for item in point_owners if _is_select_control(item)]
            if len(select_owners) == 1 and _select_has_option(select_owners[0], target_value):
                return _binding(select_owners[0])

        # A point owner without matching semantic identity is evidence of physical
        # ownership, not proof that the LLM chose the wrong business target. Keep it
        # unresolved so dispatch can fail open; only exact target-ref geometry above
        # may produce a hard contradiction.
        if point_owners and intent.family in {"activate", "navigate"}:
            return TargetBinding(
                status="unresolved",
                reason=(
                    "the action point is owned by a rendered control, but text identity "
                    "does not confirm the declared target"
                ),
            )

        # The point lands on a rendered write control but identity does not confirm the declared
        # target. This remains an identity gap because adapter labels can be wrong.
        if point_owners:
            return TargetBinding(
                status="unresolved",
                reason=(
                    "the action point is owned by a control whose identity does not "
                    "confirm the declared target"
                ),
            )
        if len(semantic) > 1:
            units = sorted({str(item.get("group_id") or "__form__") for item in semantic})
            return TargetBinding(
                status="unresolved",
                reason="the action point does not distinguish matching units: " + ", ".join(units),
            )
        return None


def _active_surface_nodes(observation: Observation) -> list[dict]:
    tree = [item for item in observation.semantic_tree or [] if isinstance(item, dict)]
    dialog_indexes = [index for index, item in enumerate(tree) if item.get("role") == "dialog"]
    if not dialog_indexes:
        return tree
    start = dialog_indexes[-1]
    depth = int(tree[start].get("depth") or 0)
    end = len(tree)
    for index in range(start + 1, len(tree)):
        if int(tree[index].get("depth") or 0) <= depth:
            end = index
            break
    return tree[start:end]


def active_surface_id(observation: Observation) -> str:
    """Stable identity of the currently active browser interaction surface."""
    nodes = _active_surface_nodes(observation)
    if nodes and nodes[0].get("role") == "dialog":
        dialog = nodes[0]
        dialog_name = str(dialog.get("key") or "").strip()
        headings = [
            str(item.get("key") or "").strip()
            for item in nodes[1:]
            if item.get("role") == "heading"
            and str(item.get("key") or "").strip()
            and str(item.get("key") or "").strip() != dialog_name
        ]
        stage = headings[-1] if headings else dialog_name
        return f"dialog:{dialog.get('ref') or dialog_name}:{stage}"

    path = urlsplit(observation.url or "").path.rstrip("/") or "/"
    heading = next(
        (
            str(item.get("key") or "").strip()
            for item in nodes
            if item.get("role") == "heading" and str(item.get("key") or "").strip()
        ),
        "",
    )
    return f"document:{path}:{heading}" if path or heading else ""


def _semantic_key(value: object) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def _choice_operation(node: dict) -> str:
    """Normalize common group commands without leaking their labels into core policy."""
    if node.get("role") != "button":
        return ""
    key = _semantic_key(node.get("key"))
    if key in {"deselectall", "unselectall", "clearall", "取消全选", "全部取消"}:
        return "clear_all"
    if key in {"selectall", "全选"}:
        return "select_all"
    return ""


def _choice_groups(nodes: list[dict]) -> list[tuple[dict[str, str], list[dict]]]:
    groups: list[tuple[dict[str, str], list[dict]]] = []
    operations: dict[str, str] = {}
    checkboxes: list[dict] = []
    for node in nodes:
        role = node.get("role")
        if role != "checkbox" and checkboxes:
            groups.append((operations, checkboxes))
            operations, checkboxes = {}, []
        if role == "checkbox":
            checkboxes.append(node)
        elif role == "button" and (operation := _choice_operation(node)):
            operations[operation] = str(node.get("key") or "").strip()
        elif role != "button":
            operations = {}
    if checkboxes:
        groups.append((operations, checkboxes))
    return groups


def active_choice_controls(
    observation: Observation,
    desired_state: dict[str, TargetValue],
) -> tuple[dict, ...]:
    """Normalize an active checkbox surface into the shared form-control contract.

    Choice groups are contiguous checkbox runs on the active browser surface. A desired value
    must resolve uniquely to one rendered checkbox run before the adapter reports anything.
    The adapter also normalizes an immediately adjacent select-all/clear-all button as an optional
    group operation. Visual-only and structurally ambiguous pages remain fail-open.
    """
    desired = [
        (str(field), tuple(_semantic_key(option) for option in options))
        for field, value in desired_state.items()
        if _semantic_key(field)
        if (options := target_value_options(value))
    ]
    if not desired:
        return ()
    nodes = _active_surface_nodes(observation)
    groups = _choice_groups(nodes)
    locations: dict[str, int] = {}
    for field, desired_keys in desired:
        matches = [
            run_index
            for run_index, (_, checkboxes) in enumerate(groups)
            if all(
                any(_semantic_key(node.get("key")) == desired_key for node in checkboxes)
                for desired_key in desired_keys
            )
        ]
        if len(matches) != 1:
            return ()
        locations[field] = matches[0]
    if len(set(locations.values())) != len(locations):
        return ()

    surface = active_surface_id(observation)
    subject_ref = f"choice:{surface}" if surface else "choice:active"
    desired_keys = {field: set(values) for field, values in desired}
    controls: list[dict] = []
    for field, run_index in locations.items():
        operations, checkboxes = groups[run_index]
        for node in checkboxes:
            label = str(node.get("key") or "").strip()
            while label and not label[0].isalnum():
                label = label[1:].lstrip()
            if not label or not _semantic_key(label):
                continue
            checked = str(node.get("value") or "").strip().casefold() in {
                "true", "1", "on", "checked", "selected",
            }
            control = {
                "kind": "checkbox_input",
                "label": label,
                "option_text": label,
                "group_field": field,
                "group_id": subject_ref,
                "checked": checked,
                "value": "on" if checked else "off",
                # Semantic-tree refs identify an option but do not carry a stable screen rect.
                # Dispatch therefore binds once to the visual point chosen for this subject.
                "binding_source": "visual",
            }
            if operations and _semantic_key(label) in desired_keys[field]:
                control["choice_operations"] = operations
            controls.append(control)
    return tuple(controls)


__all__ = [
    "active_choice_controls",
    "BrowserTargetBinder",
    "active_surface_id",
]
