"""Exact structured evidence for binding one GUI state to one target."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from gui_agent.core.schemas import Observation, StatementContract


_STATE_KEYS = frozenset({"entity", "fields"})


def _primitive_values(value: Any) -> Iterable[str | int | float | bool]:
    if isinstance(value, (str, int, float, bool)):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _primitive_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _primitive_values(item)


def _structured_values(observation: Observation) -> tuple[object, ...]:
    sources: list[Any] = [
        *(observation.semantic_tree or ()),
        *(observation.tables or ()),
        *(observation.form_control_state or observation.form_controls or ()),
    ]
    for region in observation.collection_regions or ():
        for cell in region.cells:
            sources.extend((*cell.texts, *cell.controls))
    return tuple(_primitive_values(sources))


def _contains(source: object, expected: object) -> bool:
    if type(source) is type(expected) and source == expected:
        return True
    return (
        isinstance(source, str)
        and isinstance(expected, str)
        and expected in source.split()
    )


def exact_identity_evidence(
    identity: dict[str, Any], observation: Observation,
) -> dict[str, Any]:
    """Match one already-established target identity against structured evidence."""
    if not identity:
        return {"status": "not_applicable"}
    fields = list(identity)
    if not fields:
        return {"status": "unknown", "fields": fields}
    values = _structured_values(observation)
    if not values:
        return {"status": "unknown", "fields": fields}
    missing = [
        field
        for field, value in identity.items()
        if not any(_contains(source, value) for source in values)
    ]
    return {
        "status": "matched" if not missing else "absent",
        "fields": fields,
        "missing_fields": missing,
    }


def exact_target_evidence(
    statement: StatementContract,
    observation: Observation,
) -> dict[str, Any]:
    """Match identity values declared by a target-specific reach.

    Unsupported visual-only observations return ``unknown`` so existing platform
    paths keep using Transition's visual evidence. Structured sensors never fuzzy
    normalize values: punctuation, sigils, and long source text remain exact.
    """
    target = statement.inputs.get("target")
    if not isinstance(target, dict) or not statement.expected_state:
        return {"status": "not_applicable"}
    return exact_identity_evidence({
        key: value
        for key, value in statement.expected_state.items()
        if key not in _STATE_KEYS and key in target and target[key] == value
    }, observation)


__all__ = ["exact_identity_evidence", "exact_target_evidence"]
