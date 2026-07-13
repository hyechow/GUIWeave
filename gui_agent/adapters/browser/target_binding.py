"""Optional structural target binding for browser observations."""

from __future__ import annotations

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
        semantic = [
            item
            for item in controls
            if isinstance(item, dict)
            and matches_target_control(item, step.target_control)
            and _in_declared_unit(item, step.target_group_id)
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


__all__ = [
    "BrowserTargetBinder",
    "active_surface_id",
    "active_target_aliases",
]
