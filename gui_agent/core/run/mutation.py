"""Resolve one mutation subject and authorize its next declared write."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from gui_agent.core.schemas import (
    StatementContract,
    MutationAuthorization,
    Observation,
    PolicyTurn,
    target_value_options,
)

ResolutionStatus = Literal[
    "complete", "preparing", "writable", "absent", "ambiguous", "unknown"
]
CHOICE_KINDS = ("checkbox", "radio", "switch")
WRITABLE_KINDS = ("input", "select", "textarea", *CHOICE_KINDS)
SELECTED_VALUES = {"on", "true", "checked", "selected", "1"}


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _key(value: object) -> tuple[str, ...]:
    return tuple(sorted(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", _norm(value))))


def _current(control: dict) -> str:
    return str(control.get("selected_text") or control.get("value") or "").strip()


def _is_choice(control: dict) -> bool:
    kind = _norm(control.get("kind"))
    return any(token in kind for token in CHOICE_KINDS)


def _selected(control: dict) -> bool:
    return _is_choice(control) and bool(
        control.get("checked") is True or _norm(_current(control)) in SELECTED_VALUES
    )


def _option_is(control: dict, value: str) -> bool:
    wanted = _key(value)
    return bool(
        wanted
        and any(
            _key(control.get(field)) == wanted
            for field in ("label", "key", "option_text", "text")
            if control.get(field)
        )
    )


def _satisfies(control: dict, value: str) -> bool:
    return (
        _option_is(control, value) and _selected(control)
        if _is_choice(control)
        else _norm(_current(control)) == _norm(value)
    )


def _matches_field(control: dict, field: str) -> bool:
    label = str(
        control.get("label")
        or control.get("name")
        or control.get("id")
        or control.get("placeholder")
        or ""
    ).strip()
    group = str(control.get("group_field") or "").strip()
    names = (label, group, f"{group} {label}", f"{label} {group}")
    expected = _key(field)
    return bool(expected and any(_key(name) == expected for name in names if name.strip()))


def _groups(observation: Observation) -> tuple[dict[str, list[dict]], bool]:
    groups: dict[str, list[dict]] = {}
    flat: list[dict] = []
    for control in observation.form_controls or []:
        if not isinstance(control, dict):
            continue
        group = str(control.get("group_id") or "").strip()
        (groups.setdefault(group, []) if group else flat).append(control)
    repeated = bool(groups)
    if flat:
        groups["__form__"] = flat
    return groups, repeated


DesiredState = dict[str, tuple[str, ...]]
TargetControl = tuple[str, str, dict]


def _match_targets(
    controls: list[dict], desired: DesiredState
) -> list[TargetControl] | None:
    matched: list[TargetControl] = []
    for field, values in desired.items():
        candidates = [item for item in controls if _matches_field(item, field)]
        if not candidates:
            continue
        choices = [item for item in candidates if _is_choice(item)]
        if choices:
            for value in values:
                options = [item for item in choices if _option_is(item, value)]
                if len(options) != 1:
                    return None
                matched.append((field, value, options[0]))
        elif len(values) == 1 and len(candidates) == 1:
            matched.append((field, values[0], candidates[0]))
        else:
            return None
    return matched


@dataclass(frozen=True)
class SubjectResolution:
    status: ResolutionStatus
    subject_ref: str = ""
    source: Literal["visual", "structural"] = "structural"
    next_field: str = ""
    target_control: str = ""
    next_value: str = ""
    action_family: Literal["input", "select"] = "input"
    evidence: str = ""


def _source(control: dict) -> Literal["visual", "structural"]:
    return "visual" if control.get("binding_source") == "visual" else "structural"


def _group_state(
    subject_ref: str,
    controls: list[dict],
    desired: DesiredState,
    *,
    singleton: bool,
) -> SubjectResolution | None:
    targets = _match_targets(controls, desired)
    if targets is None:
        return None
    matched_fields = {field for field, _, _ in targets}

    selected_extras: list[tuple[str, list[dict]]] = []
    cleanup_candidates: list[tuple[int, str, str, dict]] = []
    for field, values in desired.items():
        field_controls = [item for item in controls if _matches_field(item, field)]
        choices = [item for item in field_controls if _is_choice(item)]
        if not choices:
            continue
        extras = [
            item
            for item in choices
            if _selected(item)
            and not any(_option_is(item, value) for value in values)
        ]
        if extras:
            selected_extras.append((field, extras))
            pending_count = sum(
                not any(_option_is(item, value) and _selected(item) for item in choices)
                for value in values
            )
            direct_cost = len(extras) + pending_count
            field_targets = [
                control
                for target_field, _, control in targets
                if target_field == field
            ]
            operations = next(
                (
                    item.get("choice_operations") or {}
                    for item in field_targets
                    if item.get("choice_operations")
                ),
                {},
            )
            clear_label = str(operations.get("clear_all") or "").strip()
            clear_cost = 1 + len(values)
            if clear_label and clear_cost < direct_cost:
                cleanup_candidates.append(
                    (direct_cost - clear_cost, field, clear_label, field_targets[0])
                )

    if cleanup_candidates:
        _, field, label, control = max(cleanup_candidates, key=lambda item: item[0])
        return SubjectResolution(
            "preparing",
            subject_ref,
            _source(control),
            field,
            f"{field} {label}" if label else field,
            action_family="select",
            evidence=f"{subject_ref} can clear the {field} choice group before selecting its target",
        )

    if selected_extras:
        field, extras = selected_extras[0]
        extra = extras[0]
        label = str(extra.get("option_text") or extra.get("label") or "").strip()
        return SubjectResolution(
            "preparing", subject_ref, _source(extra), field,
            f"{field} {label}" if label else field,
            action_family="select",
            evidence=f"{subject_ref} has an extra selected {field} value {label!r}",
        )

    if matched_fields == set(desired) and all(
        _satisfies(control, value) for _, value, control in targets
    ):
        return SubjectResolution(
            "complete", subject_ref=subject_ref,
            evidence=f"desired state is complete on {subject_ref}",
        )

    pending = [
        (field, value, control)
        for field, value, control in targets
        if not _satisfies(control, value)
        and any(token in _norm(control.get("kind")) for token in WRITABLE_KINDS)
    ]
    if not pending:
        return None
    target_progress = any(_satisfies(control, value) for _, value, control in targets)
    all_blank = matched_fields == set(desired) and all(
        (_option_is(control, value) and not _selected(control))
        if _is_choice(control) else not _norm(_current(control))
        for _, value, control in targets
    )
    if not (singleton or target_progress or all_blank):
        return None

    field, value, control = pending[0]
    family: Literal["input", "select"] = (
        "select" if _is_choice(control) or "select" in _norm(control.get("kind")) else "input"
    )
    return SubjectResolution(
        "writable",
        subject_ref,
        _source(control),
        field,
        f"{field} {value}" if _option_is(control, value) else field,
        value,
        family,
        f"{subject_ref} owns the next desired field {field}",
    )


def _receipted_subjects(history: list[PolicyTurn], milestone_id: str) -> set[str]:
    return {
        receipt.subject_ref
        for turn in history
        if turn.action_signal is not None
        if (receipt := turn.action_signal.mutation_receipt) is not None
        if receipt.statement_id == milestone_id and receipt.subject_ref
    }


def _has_unreceipted_write(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        turn.supervisor is not None
        and turn.supervisor.milestone_id == milestone_id
        and turn.action_signal is not None
        and turn.action_signal.role == "write"
        and turn.action_signal.execution == "dispatched"
        and turn.action_signal.mutation_receipt is None
        for turn in history
    )


def _resolve(
    milestone: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
    surface_id: str,
) -> SubjectResolution:
    desired = {
        str(field): options
        for field, value in (milestone.target_values or {}).items()
        if _norm(field)
        if (options := target_value_options(value))
    }
    if not desired:
        return SubjectResolution("unknown", evidence="no desired-state map")

    groups, repeated = _groups(observation)
    bound = _receipted_subjects(history, milestone.id)
    if len(bound) > 1:
        return SubjectResolution("ambiguous", evidence="receipts bind multiple subjects")
    if bound:
        subject_ref = next(iter(bound))
        controls = groups.get(subject_ref)
        if controls is None:
            return SubjectResolution("unknown", subject_ref, evidence="bound subject is not observable")
        return _group_state(
            subject_ref, controls, desired, singleton=subject_ref == "__form__"
        ) or SubjectResolution("unknown", subject_ref, evidence="bound subject lacks declared fields")

    contaminated = _has_unreceipted_write(history, milestone.id)
    if "__form__" in groups:
        state = _group_state("__form__", groups["__form__"], desired, singleton=True)
        if state is not None:
            return SubjectResolution("unknown", evidence="unbound prior write") if contaminated else state

    states = [
        state
        for subject_ref, controls in groups.items()
        if subject_ref != "__form__"
        if (state := _group_state(subject_ref, controls, desired, singleton=False)) is not None
    ]
    preparing = [state for state in states if state.status == "preparing"]
    if len(preparing) == 1:
        return preparing[0]
    if len(preparing) > 1:
        return SubjectResolution("ambiguous", evidence="multiple subjects need preparation")

    complete = [state for state in states if state.status == "complete"]
    writable = [state for state in states if state.status == "writable"]
    if contaminated and (complete or writable):
        return SubjectResolution("ambiguous", evidence="partial state follows an unbound write")
    if complete:
        return complete[0]
    if len(writable) == 1:
        return writable[0]
    if len(writable) > 1:
        return SubjectResolution("ambiguous", evidence="multiple writable subjects")
    coverage = _norm((observation.form_controls_meta or {}).get("coverage"))
    if repeated and coverage == "complete":
        return SubjectResolution("absent", evidence="complete inventory has no target subject")
    if not observation.form_controls and len(desired) == 1:
        field, values = next(iter(desired.items()))
        if len(values) != 1:
            return SubjectResolution("unknown", evidence="multi-value target needs observable choices")
        return SubjectResolution(
            "writable", f"visual:{surface_id or milestone.id}", "visual",
            field, field, values[0], evidence="one-shot visual subject",
        )
    return SubjectResolution("unknown", evidence="subject identity is not observable")


def resolve_mutation(
    milestone: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    surface_id: str = "",
) -> SubjectResolution:
    return _resolve(milestone, observation, history, surface_id)


def authorize_mutation(
    milestone: StatementContract,
    subject: SubjectResolution,
) -> MutationAuthorization | None:
    if subject.status != "writable" or not subject.subject_ref:
        return None
    return MutationAuthorization(
        statement_id=milestone.id,
        subject_ref=subject.subject_ref,
        field=subject.next_field,
        desired_value=subject.next_value,
        source=subject.source,
    )


__all__ = [
    "SubjectResolution",
    "authorize_mutation",
    "resolve_mutation",
]
