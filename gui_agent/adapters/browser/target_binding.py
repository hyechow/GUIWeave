"""Optional structural target binding for browser observations."""

from __future__ import annotations

from itertools import groupby
from urllib.parse import urlsplit

from gui_agent.core.schemas import (
    BaseActionDecision,
    Observation,
    SupervisorStep,
    TargetBinding,
)
from .control_grounding import matches_target_control


def _point_matches(control: dict, action: object) -> bool:
    rect = control.get("rect") if isinstance(control.get("rect"), dict) else {}
    snap = getattr(action, "snap", None)
    snapped = snap.get("snapped") if isinstance(snap, dict) else None
    if isinstance(snapped, (list, tuple)) and len(snapped) == 2:
        x, y = snapped
    else:
        x = getattr(action, "x", None)
        y = getattr(action, "y", None)
    return bool(
        isinstance(x, (int, float))
        and isinstance(y, (int, float))
        and isinstance(rect.get("x"), (int, float))
        and isinstance(rect.get("y"), (int, float))
        and abs(float(rect["x"]) - float(x)) <= 3
        and abs(float(rect["y"]) - float(y)) <= 3
    )


def _in_declared_unit(control: dict, unit_hint: str) -> bool:
    if not unit_hint:
        return True
    actual = str(control.get("group_id") or "").strip()
    if unit_hint == "__form__":
        return not actual
    return actual == unit_hint


def _binding(control: dict) -> TargetBinding:
    group_id = str(control.get("group_id") or "").strip()
    return TargetBinding(
        status="bound",
        source="structural",
        unit_id=group_id or "__form__",
        reason="browser control inventory uniquely owns the concrete action point",
    )


class BrowserTargetBinder:
    """Upgrade a visual proposal only when a rendered control uniquely owns its point."""

    def bind(
        self,
        step: SupervisorStep,
        observation: Observation,
        action_decision: BaseActionDecision,
    ) -> TargetBinding | None:
        controls = getattr(observation, "form_controls", None)
        if not controls:
            return None
        authorization = step.mutation_authorization
        unit_hint = (
            authorization.subject_ref
            if authorization is not None and authorization.source == "structural"
            else ""
        )
        semantic = [
            item
            for item in controls
            if isinstance(item, dict)
            and matches_target_control(item, step.target_control)
            and _in_declared_unit(item, unit_hint)
        ]
        point_owners = [
            item
            for item in controls
            if isinstance(item, dict) and _point_matches(item, action_decision.action)
        ]
        candidates = [item for item in semantic if item in point_owners]
        if len(candidates) == 1:
            return _binding(candidates[0])
        if point_owners:
            return TargetBinding(
                status="contradicted",
                reason="the action point belongs to a different declared control or unit",
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


def active_target_aliases(observation: Observation) -> set[str]:
    """Translate browser DOM/AX inventory into platform-neutral active-target aliases."""
    aliases: set[str] = set()
    surface_nodes = _active_surface_nodes(observation)
    has_dialog = bool(surface_nodes and surface_nodes[0].get("role") == "dialog")
    sources = (surface_nodes,) if has_dialog else (
        observation.form_controls or [],
        surface_nodes,
    )
    for items in sources:
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in ("key", "label", "name", "id", "group_field"):
                value = str(item.get(field) or "").strip()
                if value:
                    aliases.add(value)
    return aliases


def _semantic_key(value: object) -> str:
    return "".join(char.casefold() for char in str(value or "") if char.isalnum())


def _checkbox_runs(nodes: list[dict]) -> list[list[dict]]:
    return [
        list(run)
        for is_checkbox, run in groupby(
            nodes, key=lambda node: node.get("role") == "checkbox"
        )
        if is_checkbox
    ]


def active_choice_controls(
    observation: Observation,
    desired_state: dict[str, str],
) -> tuple[dict, ...]:
    """Normalize an active checkbox surface into the shared form-control contract.

    Choice groups are contiguous checkbox runs on the active browser surface. A desired value
    must resolve uniquely to one rendered checkbox run before the adapter reports anything.
    The adapter exposes concrete options only; it does not infer workflow commands from nearby
    buttons. Visual-only and structurally ambiguous pages therefore remain fail-open.
    """
    desired = [
        (str(field), _semantic_key(value))
        for field, value in desired_state.items()
        if _semantic_key(field) and _semantic_key(value)
    ]
    if not desired:
        return ()
    nodes = _active_surface_nodes(observation)
    runs = _checkbox_runs(nodes)
    locations: dict[str, int] = {}
    for field, desired_key in desired:
        matches = [
            run_index
            for run_index, checkboxes in enumerate(runs)
            if any(_semantic_key(node.get("key")) == desired_key for node in checkboxes)
        ]
        if len(matches) != 1:
            return ()
        locations[field] = matches[0]
    if len(set(locations.values())) != len(locations):
        return ()

    surface = active_surface_id(observation)
    subject_ref = f"choice:{surface}" if surface else "choice:active"
    controls: list[dict] = []
    for field, run_index in locations.items():
        for node in runs[run_index]:
            label = str(node.get("key") or "").strip()
            while label and not label[0].isalnum():
                label = label[1:].lstrip()
            if not label or not _semantic_key(label):
                continue
            checked = str(node.get("value") or "").strip().casefold() in {
                "true", "1", "on", "checked", "selected",
            }
            controls.append({
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
            })
    return tuple(controls)


__all__ = [
    "active_choice_controls",
    "BrowserTargetBinder",
    "active_surface_id",
    "active_target_aliases",
]
