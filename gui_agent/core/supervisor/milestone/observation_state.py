from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from gui_agent.core.run.execution_signals import target_matches_declared
from gui_agent.core.schemas import Milestone

# ── filter "action-applied" gate ──────────────────────────────────────────────
# A `filter` milestone's job is to APPLY a filter; its success is "the intended filter is in
# effect" — which the adapter reports authoritatively through Observation.applied_filters
# (whatever platform-native evidence channel produced it), NOT by re-reading row/cell values.
# Decouples action-applied from effect-judgment so the checker can't reject a correctly-applied
# filter on display-column grounds.

@dataclass(frozen=True)
class RuntimeFilterIntent:
    """The concrete control/value pair actually written for the current filter attempt."""

    target_control: str
    target_value: str


def _value_tokens(s: str) -> list[str]:
    """Alphanumeric tokens of a filter value, lowercased (drops separators like ' - ', ':')."""
    return [t.lower() for t in re.findall(r"[A-Za-z0-9.]+", s or "")]


def _effective_filter_intents(
    milestone: Milestone,
    runtime_intent: RuntimeFilterIntent | None,
) -> list[RuntimeFilterIntent]:
    """Resolve semantic filter declarations to the concrete controls used this attempt."""
    declared = [
        RuntimeFilterIntent(str(control), str(value))
        for control, value in (milestone.target_values or {}).items()
        if str(control).strip() and str(value).strip()
    ]
    if runtime_intent is None:
        return declared
    if not declared:
        return [runtime_intent]

    # The DSL names semantic fields while the UI may offer a different concrete search control.
    # A dispatched write receipt may refine exactly one declaration with the same desired value;
    # it may neither invent a value nor choose between equal-valued declarations.
    runtime_value = sorted(_value_tokens(runtime_intent.target_value))
    matching_indexes = [
        index
        for index, intent in enumerate(declared)
        if sorted(_value_tokens(intent.target_value)) == runtime_value
    ]
    if len(matching_indexes) != 1:
        return declared
    resolved = list(declared)
    resolved[matching_indexes[0]] = runtime_intent
    return resolved


def observed_filter_intent(
    applied_filters: Optional[dict[str, str]],
    form_controls: list[dict] | None,
    milestone: Milestone,
) -> RuntimeFilterIntent | None:
    """Bind an applied-state entry to its currently populated concrete filter control.

    This covers an execution unit entered with its desired filter already active.  Both sides of
    the adapter state must agree: one applied entry and one rendered control have compatible
    identities and the same exact value.  The concrete value must then refine exactly one DSL
    declaration, using the same ambiguity rules as an action receipt.
    """
    if not applied_filters or not form_controls or not milestone.target_values:
        return None
    candidates: list[RuntimeFilterIntent] = []
    for applied_label, applied_value in applied_filters.items():
        wanted = sorted(_value_tokens(applied_value))
        controls: list[str] = []
        for item in form_controls:
            if not isinstance(item, dict) or item.get("group_id"):
                continue
            control = str(
                item.get("label") or item.get("name") or item.get("id") or ""
            ).strip()
            value = str(item.get("selected_text") or item.get("value") or "").strip()
            if (
                control
                and sorted(_value_tokens(value)) == wanted
                and target_matches_declared(applied_label, (control,))
            ):
                controls.append(control)
        if len(controls) == 1:
            candidates.append(RuntimeFilterIntent(controls[0], str(applied_value)))
    resolved = [
        candidate
        for candidate in candidates
        if _effective_filter_intents(milestone, candidate)
        != _effective_filter_intents(milestone, None)
    ]
    return resolved[0] if len(resolved) == 1 else None


def _matched_applied_filter_labels(
    applied_filters: Optional[dict[str, str]],
    milestone: Milestone,
    runtime_intent: RuntimeFilterIntent | None = None,
) -> set[str]:
    if not applied_filters:
        return set()
    intents = _effective_filter_intents(milestone, runtime_intent)
    if not intents:
        return set()

    matched: set[str] = set()
    for intent in intents:
        wanted = sorted(_value_tokens(intent.target_value))
        candidates = {
            label
            for label, value in applied_filters.items()
            if target_matches_declared(label, (intent.target_control,))
            and sorted(_value_tokens(value)) == wanted
        }
        if len(candidates) != 1:
            return set()
        matched.update(candidates)
    return matched


def filter_chips_clean(
    applied_filters: Optional[dict[str, str]],
    milestone: Milestone,
    runtime_intent: RuntimeFilterIntent | None = None,
) -> bool:
    """Whether the live filter set exactly matches the declared filter-state contract."""
    matched_labels = _matched_applied_filter_labels(
        applied_filters, milestone, runtime_intent
    )
    if not matched_labels:
        return False
    return set(applied_filters or {}) == matched_labels


def filter_residual_labels(
    applied_filters: Optional[dict[str, str]],
    milestone: Milestone,
    runtime_intent: RuntimeFilterIntent | None = None,
) -> list[str]:
    """Return live filters not present in the complete declared filter-state contract."""
    if not applied_filters:
        return []
    if not milestone.target_values:
        return []
    matched_labels = _matched_applied_filter_labels(
        applied_filters, milestone, runtime_intent
    )
    declared_controls = tuple(
        intent.target_control
        for intent in _effective_filter_intents(milestone, runtime_intent)
    )
    return [
        label
        for label in applied_filters
        if label not in matched_labels
        and not target_matches_declared(label, declared_controls)
    ]


def filter_state_satisfies_target(
    applied_filters: Optional[dict[str, str]],
    milestone: Milestone,
    runtime_intent: RuntimeFilterIntent | None = None,
) -> bool:
    """Whether every declared filter pair is present in the adapter's applied state."""
    return bool(_matched_applied_filter_labels(
        applied_filters, milestone, runtime_intent
    ))



def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _normalize_field_name(field: str) -> str:
    return str(field or "").strip(" '\"「」『』")


def _field_aliases(field: str) -> set[str]:
    norm = _norm_text(_normalize_field_name(field))
    return {norm} if norm else set()


def _extract_target_fields(milestone: Milestone) -> list[str]:
    fields: list[str] = []
    for raw in [
        *(milestone.target_values or {}).keys(),
        *(milestone.target_controls or []),
    ]:
        field = _normalize_field_name(str(raw or ""))
        if field and field not in fields:
            fields.append(field)
    return fields[:3]


def _control_label(item: dict) -> str:
    return str(item.get("label") or item.get("name") or item.get("id") or item.get("placeholder") or "").strip()


def _visible_field_controls(form_controls: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for item in form_controls or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if "input" not in kind and "select" not in kind and "textarea" not in kind:
            continue
        label = _control_label(item)
        label_norm = _norm_text(label)
        if not label_norm:
            continue
        out.append(item)
    return out


def _find_matching_control(field: str, controls: list[dict]) -> dict | None:
    """Resolve a declared control name against adapter-provided control labels."""
    aliases = _field_aliases(field)
    for item in controls:
        label_norm = _norm_text(_control_label(item))
        if label_norm and label_norm in aliases:
            return item
    return None


def _control_current_value(item: dict) -> str:
    """Read the adapter's normalized current-value channels."""
    return str(item.get("selected_text") or item.get("value") or "").strip()


def required_group_field_gaps(
    form_controls: list[dict] | None,
    milestone: Milestone,
) -> list[str]:
    """Return required gaps only inside the explicitly identified target unit.

    Free-text or adjacent values are not unit identity. Without ``target_values`` and one uniquely
    classified group, a required field elsewhere on the page cannot constrain this milestone.
    """
    if not milestone.target_values:
        return []
    state = target_unit_state(form_controls, milestone)
    if state.status not in {"complete", "partial", "unique_blank"} or not state.group_id:
        return []
    gaps: list[str] = []
    for item in form_controls or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "").strip() or "__form__"
        if group_id != state.group_id or item.get("required") is not True:
            continue
        current = str(item.get("selected_text") or item.get("value") or "").strip()
        if current:
            continue
        label = str(
            item.get("group_field")
            or item.get("label")
            or item.get("name")
            or "required field"
        ).strip()
        if label and label not in gaps:
            gaps.append(label)
    return gaps


@dataclass(frozen=True)
class TargetUnitState:
    """State of one structural unit that owns all declared target fields."""

    status: Literal[
        "complete", "partial", "unique_blank", "absent", "ambiguous", "unknown"
    ]
    group_id: str = ""
    missing_fields: tuple[str, ...] = ()
    writable_fields: tuple[str, ...] = ()
    next_field: str = ""
    next_value: str = ""
    evidence: str = ""


def _control_semantic_names(item: dict) -> set[str]:
    label = _control_label(item)
    group_field = str(item.get("group_field") or "").strip()
    names = {label}
    if label and group_field:
        names.add(f"{group_field} {label}")
        names.add(f"{label} {group_field}")
    elif group_field:
        names.add(group_field)
    return {_norm_text(value) for value in names if _norm_text(value)}


def _target_control_matches(item: dict, target_key: str) -> bool:
    aliases = _control_semantic_names(item)
    if target_key in aliases:
        return True
    # A planner may append a structural annotation to an exact field name. Only a compound alias
    # may match that annotation; a generic label such as "Description" must not select one of
    # several grouped Description columns.
    compound = {
        alias
        for alias in aliases
        if _norm_text(str(item.get("group_field") or "")) in alias
        and _norm_text(str(item.get("group_field") or ""))
    }
    return any(alias in target_key for alias in compound)


def _next_target_field(
    missing: tuple[str, ...],
    matched: dict[str, dict],
) -> str:
    for field in missing:
        item = matched.get(_norm_text(field))
        if item is not None and item.get("required") is True:
            return field
    return missing[0] if missing else ""


def target_unit_state(
    form_controls: list[dict] | None,
    milestone: Milestone,
    *,
    coverage: str = "unknown",
) -> TargetUnitState:
    return _target_unit_state(form_controls, milestone, coverage=coverage)


def _target_unit_state(
    form_controls: list[dict] | None,
    milestone: Milestone,
    *,
    coverage: str,
) -> TargetUnitState:
    """Classify the exact structural unit that owns declared target fields.

    The classifier never uses adjacent values, visual distance, row order, or latest-row guesses.
    A repeated unit is writable only when it is the sole partial unit or the sole completely blank
    unit exposing every declared field. Multiple candidates are an explicit ambiguity.
    """
    targets = [
        (_norm_text(field), field, str(value), _norm_text(value))
        for field, value in (milestone.target_values or {}).items()
        if _norm_text(field) and _norm_text(value)
    ]
    if not targets:
        return TargetUnitState("unknown")

    groups: dict[str, list[dict]] = {}
    flat: list[dict] = []
    for item in form_controls or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "").strip()
        if group_id:
            groups.setdefault(group_id, []).append(item)
        else:
            flat.append(item)
    declared_controls = {_norm_text(value) for value in milestone.target_controls or []}
    flat_is_owned = len(targets) == 1 or all(
        any(
            target_key == declared
            or target_key in declared
            or declared in target_key
            for declared in declared_controls
            if declared
        )
        for target_key, _field, _raw, _value in targets
    )
    if flat and flat_is_owned:
        groups["__form__"] = flat

    complete: list[TargetUnitState] = []
    partial: list[TargetUnitState] = []
    blank: list[TargetUnitState] = []
    structurally_ambiguous = False
    for group_id, items in groups.items():
        matched: dict[str, dict] = {}
        for target_key, _field, _raw_value, _target_value in targets:
            candidates = [item for item in items if _target_control_matches(item, target_key)]
            if len(candidates) > 1:
                structurally_ambiguous = True
                continue
            if candidates:
                matched[target_key] = candidates[0]
        equal_fields = {
            target_key
            for target_key, item in matched.items()
            if _norm_text(_control_current_value(item))
            == next(value for key, _field, _raw, value in targets if key == target_key)
        }
        missing = tuple(
            field
            for target_key, field, _raw_value, _target_value in targets
            if target_key not in equal_fields
        )
        if not missing:
            complete.append(TargetUnitState(
                "complete",
                group_id=group_id,
                evidence=f"目标字段在同一结构单元 {group_id} 中均已达到声明值",
            ))
            continue
        writable = tuple(
            field
            for target_key, field, _raw_value, _target_value in targets
            if (item := matched.get(target_key)) is not None
            if target_key not in equal_fields
            and any(token in str(item.get("kind") or "").lower() for token in ("input", "select", "textarea"))
        )
        next_field = _next_target_field(missing, matched)
        next_value = next(
            (raw for _key, field, raw, _norm in targets if field == next_field), ""
        )
        state = TargetUnitState(
            "partial",
            group_id=group_id,
            missing_fields=missing,
            writable_fields=writable,
            next_field=next_field,
            next_value=next_value,
            evidence=(
                f"目标结构单元 {group_id} 已定位，但字段未达到声明值："
                + "、".join(missing)
            ),
        )
        all_fields_present = len(matched) == len(targets)
        all_declared_blank = all(
            not _norm_text(_control_current_value(matched[target_key]))
            for target_key, _field, _raw, _value in targets
            if target_key in matched
        ) and all_fields_present
        if equal_fields or (group_id == "__form__" and all_fields_present):
            partial.append(state)
        elif all_declared_blank:
            blank.append(TargetUnitState(
                "unique_blank",
                group_id=state.group_id,
                missing_fields=state.missing_fields,
                writable_fields=state.writable_fields,
                next_field=state.next_field,
                next_value=state.next_value,
                evidence=f"结构单元 {group_id} 是唯一候选空白单元",
            ))

    if complete:
        return complete[0]
    if structurally_ambiguous or len(partial) > 1 or (not partial and len(blank) > 1):
        return TargetUnitState(
            "ambiguous",
            evidence="存在多个满足声明字段结构的候选单元，不能确定性选择写入目标",
        )
    if len(partial) == 1:
        return partial[0]
    if len(blank) == 1:
        return blank[0]
    if coverage.lower() == "complete":
        return TargetUnitState(
            "absent",
            evidence="完整控件清单中没有可唯一识别的目标结构单元",
        )
    return TargetUnitState("unknown")

