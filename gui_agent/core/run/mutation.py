"""Project declared target-value evidence from observations and Journal receipts.

This module answers whether the declared business state is visible on one bound subject. It
does not choose a field, control, cleanup operation, or next action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from gui_agent.core.schemas import (
    Observation,
    PolicyTurn,
    StatementContract,
    target_value_options,
)

ResolutionStatus = Literal["complete", "writable", "absent", "ambiguous", "unknown"]
CHOICE_KINDS = ("checkbox", "radio", "switch")
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
    if _is_choice(control):
        return _option_is(control, value) and _selected(control)
    return _norm(_current(control)) == _norm(value)


def _matches_field(control: dict, field: str) -> bool:
    label = str(
        control.get("label")
        or control.get("name")
        or control.get("id")
        or control.get("placeholder")
        or ""
    ).strip()
    group = str(control.get("group_field") or "").strip()
    expected = _key(field)
    names = (label, group, f"{group} {label}", f"{label} {group}")
    return bool(expected and any(_key(name) == expected for name in names if name))


DesiredState = dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SubjectResolution:
    status: ResolutionStatus
    subject_ref: str = ""
    evidence: str = ""


def _desired_state(statement: StatementContract) -> DesiredState:
    desired = {
        str(field): options
        for field, value in (statement.target_values or {}).items()
        if _norm(field)
        if (options := target_value_options(value))
    }
    if len(desired) != 1:
        return desired

    abstract_field, values = next(iter(desired.items()))
    concrete_fields = tuple(dict.fromkeys(filter(None, map(str.strip, statement.target_controls))))
    # A collection key may carry aligned members while target_controls names their row fields.
    if (
        len(values) > 1
        and len(concrete_fields) > 1
        and all(_key(field) != _key(abstract_field) for field in concrete_fields)
    ):
        return {field: values for field in concrete_fields}
    return desired


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


def _group_resolution(
    subject_ref: str,
    controls: list[dict],
    desired: DesiredState,
    *,
    singleton: bool,
) -> SubjectResolution | None:
    targets: list[tuple[str, str, dict]] = []
    for field, values in desired.items():
        candidates = [control for control in controls if _matches_field(control, field)]
        if not candidates:
            return None
        choices = [control for control in candidates if _is_choice(control)]
        if choices:
            for value in values:
                matches = [control for control in choices if _option_is(control, value)]
                if len(matches) != 1:
                    return None
                targets.append((field, value, matches[0]))
        elif len(values) == 1 and len(candidates) == 1:
            targets.append((field, values[0], candidates[0]))
        else:
            return None

    selected_extras = any(
        _selected(control)
        and not any(_option_is(control, value) for value in desired[field])
        for field in desired
        for control in controls
        if _matches_field(control, field) and _is_choice(control)
    )
    satisfied = [
        _satisfies(control, value) for _, value, control in targets
    ]
    if all(satisfied) and not selected_extras:
        return SubjectResolution(
            "complete",
            subject_ref,
            f"desired state is complete on {subject_ref}",
        )

    blank = all(
        not _selected(control) if _is_choice(control) else not _norm(_current(control))
        for _, _, control in targets
    )
    if singleton or any(satisfied) or blank or selected_extras:
        return SubjectResolution(
            "writable",
            subject_ref,
            f"declared target state is not complete on {subject_ref}",
        )
    return None


def _collection_complete(
    groups: dict[str, list[dict]], desired: DesiredState
) -> bool:
    """Whether aligned list values are present across distinct repeated members."""
    widths = {len(values) for values in desired.values() if len(values) > 1}
    if len(widths) != 1:
        return False
    width = next(iter(widths), 0)
    if width < 2 or any(len(values) not in {1, width} for values in desired.values()):
        return False
    remaining = {key: value for key, value in groups.items() if key != "__form__"}
    for index in range(width):
        row = {
            field: (values[index] if len(values) == width else values[0],)
            for field, values in desired.items()
        }
        matched = next(
            (
                subject_ref
                for subject_ref, controls in remaining.items()
                if (state := _group_resolution(
                    subject_ref, controls, row, singleton=False
                )) is not None
                and state.status == "complete"
            ),
            None,
        )
        if matched is None:
            return False
        remaining.pop(matched)
    return True


def _receipted_subjects(history: list[PolicyTurn], statement_id: str) -> set[str]:
    return {
        receipt.subject_ref
        for turn in history
        if turn.action_signal is not None
        if (receipt := turn.action_signal.mutation_receipt) is not None
        if receipt.statement_id == statement_id and receipt.subject_ref
    }


def resolve_mutation(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
) -> SubjectResolution:
    desired = _desired_state(statement)
    if not desired:
        return SubjectResolution("unknown", evidence="no desired-state map")

    groups, repeated = _groups(observation)
    coverage = _norm((observation.form_controls_meta or {}).get("coverage"))
    if _collection_complete(groups, desired):
        return SubjectResolution(
            "complete",
            evidence="desired state is complete across the repeated collection",
        )

    bound = _receipted_subjects(history, statement.id)
    if len(bound) > 1:
        return SubjectResolution("ambiguous", evidence="receipts bind multiple subjects")
    if bound:
        subject_ref = next(iter(bound))
        controls = groups.get(subject_ref)
        if controls is None:
            return SubjectResolution(
                "unknown", subject_ref, "bound subject is not observable"
            )
        return _group_resolution(
            subject_ref, controls, desired, singleton=subject_ref == "__form__"
        ) or SubjectResolution(
            "unknown", subject_ref, "bound subject lacks declared fields"
        )

    if "__form__" in groups:
        state = _group_resolution("__form__", groups["__form__"], desired, singleton=True)
        if state is not None:
            return state

    states = [
        state
        for subject_ref, controls in groups.items()
        if subject_ref != "__form__"
        if (state := _group_resolution(
            subject_ref, controls, desired, singleton=False
        )) is not None
    ]
    complete = [state for state in states if state.status == "complete"]
    writable = [state for state in states if state.status == "writable"]
    if complete:
        return complete[0]
    if len(writable) == 1:
        return writable[0]
    if len(writable) > 1:
        return SubjectResolution("ambiguous", evidence="multiple writable subjects")
    if (
        repeated
        and coverage in {"complete", "full"}
        and any(
            _matches_field(control, field)
            for controls in groups.values()
            for control in controls
            for field in desired
        )
    ):
        return SubjectResolution("absent", evidence="complete inventory has no target subject")
    return SubjectResolution("unknown", evidence="subject identity is not observable")


__all__ = ["SubjectResolution", "resolve_mutation"]
