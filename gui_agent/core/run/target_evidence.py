"""Exact structured evidence for binding one GUI state to one target."""

from __future__ import annotations

from collections import Counter
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


def _is_collection_member(
    identity: dict[str, Any], observation: Observation,
) -> bool:
    """Whether a cell carrying target identity has a structural peer."""
    for region in observation.collection_regions or ():
        cells = [
            (
                cell.structural_key,
                tuple(_primitive_values((*cell.texts, *cell.controls))),
            )
            for cell in region.cells
        ]
        values = tuple(source for _, items in cells for source in items)
        if not all(
            any(_contains(source, value) for source in values)
            for value in identity.values()
        ):
            continue
        key_counts = Counter(key for key, _ in cells)
        matching_keys = {
            key
            for key, items in cells
            if any(
                _contains(source, value)
                for source in items
                for value in identity.values()
            )
        }
        if any(key_counts[key] > 1 for key in matching_keys):
            return True
    return False


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
    result = {
        "status": "matched" if not missing else "absent",
        "fields": fields,
        "missing_fields": missing,
    }
    if not missing and _is_collection_member(identity, observation):
        result["collection_member"] = True
    return result


def exact_target_evidence(
    statement: StatementContract,
    observation: Observation,
) -> dict[str, Any]:
    """Match exact target identity from Reach or its Commit ``ui_state``."""
    target = statement.inputs.get("target")
    ui_state = statement.inputs.get("ui_state")
    state = statement.expected_state or (
        ui_state.get("postcondition") if isinstance(ui_state, dict) else None
    )
    if not isinstance(target, dict) or not isinstance(state, dict) or not state:
        return {"status": "not_applicable"}
    return exact_identity_evidence({
        key: value
        for key, value in state.items()
        if key not in _STATE_KEYS and key in target and target[key] == value
    }, observation)


__all__ = ["exact_identity_evidence", "exact_target_evidence"]
