from __future__ import annotations

from typing import Optional

from gui_agent.core.schemas import Milestone

from .observation_state import (
    _control_label,
    _extract_target_fields,
    _find_matching_control,
    _norm_text,
    _target_control_matches,
    _visible_field_controls,
    target_unit_state,
)
from .schemas import _PlanResult

_TARGET_AFFORDANCE_KINDS = (
    "input",
    "select",
    "textarea",
    "combobox",
    "listbox",
    "checkbox",
    "radio",
    "switch",
)
_SECTION_TOGGLE_KINDS = (
    "section_toggle",
    "accordion",
    "tab",
    "treeitem",
)


def _control_is_named_in_milestone(item: dict, milestone: Milestone) -> bool:
    declared = {_norm_text(field) for field in _extract_target_fields(milestone)}
    if not declared:
        return False
    observed = {
        _norm_text(str(raw or ""))
        for raw in (
            item.get("label"),
            item.get("name"),
            item.get("id"),
            item.get("placeholder"),
        )
        if str(raw or "").strip()
    }
    return bool(declared & observed)


def target_affordance_scroll_plan(
    form_controls: Optional[list[dict]],
    milestone: Milestone,
) -> Optional[_PlanResult]:
    """Return a deterministic acquire scroll when target controls already exist offscreen.

    This is the form-control sibling of FilterGate / CheckboxGate: if the adapter has already
    reported a target affordance in ``obs.form_controls`` with an off-viewport direction, the next
    operation is a deterministic page-internal acquire action. Do not let the vision checker turn
    "not visible in screenshot" into a speculative route change. Conservative boundary: only
    action/filter milestones, only named controls mentioned by the milestone, and only while at
    least one matched target control remains outside the viewport in a single known direction.
    """
    if milestone.kind not in {"action", "filter"}:
        return None
    controls = _visible_field_controls(form_controls)
    if not controls:
        return None
    matched: list[dict] = []
    for item in controls:
        kind = str(item.get("kind") or "").lower()
        if not any(part in kind for part in _TARGET_AFFORDANCE_KINDS):
            continue
        if _control_is_named_in_milestone(item, milestone):
            matched.append(item)
    if not matched:
        return None
    offscreen = [
        item
        for item in matched
        if item.get("in_viewport") is False and item.get("viewport_pos") in {"above", "below"}
    ]
    if not offscreen:
        return None
    directions = {
        "up" if item.get("viewport_pos") == "above" else "down"
        for item in offscreen
    }
    if len(directions) != 1:
        return None
    direction = next(iter(directions))
    target = offscreen[0]
    label = _control_label(target)
    rect = target.get("rect") if isinstance(target.get("rect"), dict) else {}
    y = rect.get("y") if isinstance(rect, dict) else None
    direction_text = "向上" if direction == "up" else "向下"
    suffix = f"（DOM center y={y}）" if isinstance(y, int) else ""
    return _PlanResult(
        instruction=f"{direction_text}滚动到「{label}」控件附近{suffix}",
        summary=(
            f"目标控件「{label}」已由 DOM 确认存在但不在当前视口；"
            f"先{direction_text}滚动完成页内 acquire，不切换页面模式。"
        ),
        atomic_role="iterate",
        action_family="iterate",
        direction=direction,
    )


def target_section_acquire_plan(
    form_controls: Optional[list[dict]],
    milestone: Milestone,
) -> Optional[_PlanResult]:
    """Return a deterministic acquire action for a named section/tab/accordion.

    Some pages render fields only after their containing section is expanded, so the target field
    is legitimately absent from ``form_controls`` until the section is opened. This is the same
    acquire phase as offscreen-control scrolling, but the affordance is a section header instead
    of the final input. The rule is intentionally structural and conservative: the milestone must
    explicitly name the section/toggle, and the adapter must expose that toggle as a DOM fact.
    """
    if milestone.kind not in {"action", "filter"}:
        return None

    def _is_section_toggle(item: dict) -> bool:
        kind = str(item.get("kind") or "").lower()
        return any(part in kind for part in _SECTION_TOGGLE_KINDS)

    def _is_expanded(item: dict) -> bool:
        value = str(item.get("selected_text") or item.get("value") or "").strip().lower()
        return value in {"1", "true", "yes", "open", "opened", "expanded", "on"}

    def _is_collapsed(item: dict) -> bool:
        # Explicit collapsed signal ONLY — an UNKNOWN state ('' from form_reader, e.g. a custom
        # accordion with no aria-expanded/recognizable class) is NOT collapsed. The acquire click
        # must fire only on a definitely-collapsed section: clicking an unknown (possibly already
        # open) section would collapse it and hide the target, and re-firing every turn on an
        # unrecognized state is a toggle loop (review findings M1/M2).
        value = str(item.get("selected_text") or item.get("value") or "").strip().lower()
        return value in {"0", "false", "no", "closed", "collapsed", "off", "hidden"}

    def _click_section_plan(item: dict) -> _PlanResult:
        label = _control_label(item)
        return _PlanResult(
            instruction=f"点击或展开「{label}」区域",
            summary=(
                f"目标字段尚未作为控件出现，但页面暴露了页内区域「{label}」；"
                "先展开该区域完成 affordance acquire。"
            ),
            action_family="activate",
        )

    def _scroll_section_plan(item: dict) -> _PlanResult:
        direction = "up" if item.get("viewport_pos") == "above" else "down"
        label = _control_label(item)
        direction_text = "向上" if direction == "up" else "向下"
        return _PlanResult(
            instruction=f"{direction_text}滚动到「{label}」区域",
            summary=(
                f"目标相关区域「{label}」已由 DOM 确认存在但不在当前视口；"
                f"先{direction_text}滚动到该页内入口。"
            ),
            atomic_role="iterate",
            action_family="iterate",
            direction=direction,
        )

    toggles = [
        item
        for item in form_controls or []
        if isinstance(item, dict) and _is_section_toggle(item) and _control_label(item)
    ]
    candidates: list[dict] = []
    for item in toggles:
        if _control_is_named_in_milestone(item, milestone):
            candidates.append(item)
    target_fields = _extract_target_fields(milestone)
    target_controls: list[dict] = []
    if target_fields:
        controls = _visible_field_controls(form_controls)
        for field in target_fields:
            match = _find_matching_control(field, controls)
            if match is not None:
                target_controls.append(match)
    # Once the final target field/editor is already in the current viewport, acquire is normally
    # done. An explicitly named section BELOW the viewport is the exception: a visible same-name
    # field appears before that section in DOM order and therefore cannot be its descendant. In
    # that case the container identity wins, preventing a flat control inventory from confusing a
    # parent-form field with a field inside the requested section/wizard. A section header above
    # the viewport may legitimately contain a currently visible descendant, so keep the old
    # short-circuit for that direction.
    target_visible = any(item.get("in_viewport") is not False for item in target_controls)
    named_section_below = any(
        item.get("in_viewport") is False and item.get("viewport_pos") == "below"
        for item in candidates
    )
    if target_visible and not named_section_below:
        return None
    if not candidates:
        return None

    # Prefer a visible EXPLICITLY-collapsed section: clicking it reveals the target controls. An
    # unknown-state section is skipped here (not clicked) — see _is_collapsed: clicking a possibly
    # already-open section would collapse it / loop (M1/M2).
    for item in candidates:
        if item.get("in_viewport") is False:
            continue
        if not _is_collapsed(item):
            continue
        return _click_section_plan(item)

    # If the named section exists but is offscreen, scroll toward the section before asking the
    # planner/checker to invent another route. Expanded only describes disclosure state; it does
    # not mean the header/content is in the viewport, so expanded offscreen sections still need
    # acquire scrolling.
    offscreen = [
        item
        for item in candidates
        if (
            item.get("in_viewport") is False
            and item.get("viewport_pos") in {"above", "below"}
        )
    ]
    if not offscreen:
        return None
    directions = {
        "up" if item.get("viewport_pos") == "above" else "down"
        for item in offscreen
    }
    if len(directions) != 1:
        return None
    return _scroll_section_plan(offscreen[0])


def target_unit_write_plan(
    form_controls: list[dict] | None,
    milestone: Milestone,
    *,
    coverage: str = "unknown",
) -> _PlanResult | None:
    """Plan the next declared field write when one structural unit is unambiguous."""
    state = target_unit_state(form_controls, milestone, coverage=coverage)
    if (
        state.status not in {"partial", "unique_blank"}
        or not state.group_id
        or not state.next_field
        or state.next_field not in state.writable_fields
    ):
        return None
    target_key = _norm_text(state.next_field)
    candidates = [
        item
        for item in form_controls or []
        if isinstance(item, dict)
        and (str(item.get("group_id") or "").strip() or "__form__") == state.group_id
        and _target_control_matches(item, target_key)
    ]
    if len(candidates) != 1:
        return None
    kind = str(candidates[0].get("kind") or "").lower()
    family = (
        "select"
        if any(token in kind for token in ("select", "checkbox", "radio", "switch"))
        else "input"
    )
    return _PlanResult(
        instruction=f"将「{state.next_field}」设置为「{state.next_value}」",
        summary=state.evidence,
        atomic_role="write",
        action_family=family,
        target_control=state.next_field,
        target_value=state.next_value,
        target_group_id=state.group_id,
    )

