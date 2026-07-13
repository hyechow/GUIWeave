from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

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
    runtime_intent: RuntimeFilterIntent | None = None,
) -> RuntimeFilterIntent | None:
    """Bind an applied-state entry to its currently populated concrete filter control.

    This covers an execution unit entered with its desired filter already active.  Both sides of
    the adapter state must agree: one applied entry and one rendered control have compatible
    identities and the same exact value. The concrete value must refine exactly one DSL
    declaration; when the DSL has no field map, it must instead equal the current attempt's write
    receipt. Ambiguous state never changes the receipt identity.
    """
    if not applied_filters or not form_controls:
        return None
    if not milestone.target_values and runtime_intent is None:
        return None
    desired_values = {
        tuple(sorted(_value_tokens(str(value))))
        for value in (milestone.target_values or {}).values()
        if str(value).strip()
    }
    if runtime_intent is not None:
        desired_values.add(tuple(sorted(_value_tokens(runtime_intent.target_value))))
    candidates: list[RuntimeFilterIntent] = []
    for applied_label, applied_value in applied_filters.items():
        wanted = sorted(_value_tokens(applied_value))
        if tuple(wanted) not in desired_values:
            continue
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
    if not milestone.target_values:
        # Without a declared field/value map, the write receipt supplies the desired value while
        # the current adapter state supplies the concrete control identity. Both must resolve to
        # one exact pair; otherwise retain the receipt and let normal checking continue.
        return candidates[0] if len(candidates) == 1 else None
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

