"""Exact structured evidence for binding one GUI state to one target."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from gui_agent.core.schemas import Observation, StatementContract


_STATE_KEYS = frozenset({"entity", "fields"})
# Free-text row payloads must not become complete-gate identity (detail pages
# rarely re-expose the full body/snippet string the list row carried).
_CONTENT_KEYS = frozenset({
    "body",
    "content",
    "text",
    "description",
    "message",
    "snippet",
    "preview",
    "subject",
})
# Phone / long numeric ids: require enough digits so short numbers never loose-match.
_MIN_PHONE_DIGITS = 7
_DIGIT_RE = re.compile(r"\d+")


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


def _tokens(text: str) -> list[str]:
    """Whitespace tokens with light edge punctuation stripped."""
    out: list[str] = []
    for raw in text.split():
        token = raw.strip(".,;:!?\"'`")
        if token:
            out.append(token)
    return out


def _digits(text: str) -> str:
    return "".join(_DIGIT_RE.findall(text))


def _contains(source: object, expected: object) -> bool:
    """Whether structured ``source`` carries identity value ``expected``.

    Matching is value-based (not field-name-based):

    1. typed equality
    2. multi-word / punctuated strings as a consecutive token run
       (fixes ``(505) 123-4567`` vs ``Texting with (505) 123-4567 (SMS/MMS)``)
    3. phone-like digit runs (length >= 7) as digit-subsequence
    """
    if type(source) is type(expected) and source == expected:
        return True
    if not isinstance(source, str) or not isinstance(expected, str):
        return False
    if not expected:
        return False
    if source == expected:
        return True

    source_tokens = _tokens(source)
    expected_tokens = _tokens(expected)
    if expected_tokens:
        # Whole expected as one whitespace token (legacy path), but refuse short
        # pure-digit tokens so "11" never matches "order 11 items".
        if len(expected_tokens) == 1:
            token = expected_tokens[0]
            digits_only = _digits(token) == token and token.isdigit()
            if digits_only and len(token) < _MIN_PHONE_DIGITS:
                pass  # fall through to exact-only (already failed above)
            elif token in source_tokens:
                return True
        # Consecutive token run: "(505)" "123-4567" inside header tokens.
        n = len(expected_tokens)
        if n >= 2:
            for index in range(0, len(source_tokens) - n + 1):
                if source_tokens[index : index + n] == expected_tokens:
                    return True
        elif n == 1:
            # single non-short-digit token already handled; multi-char words ok
            pass

    expected_digits = _digits(expected)
    if len(expected_digits) >= _MIN_PHONE_DIGITS:
        source_digits = _digits(source)
        if expected_digits and expected_digits in source_digits:
            return True
    return False


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


def _primitive_identity(target: dict[str, Any]) -> dict[str, Any]:
    """Row-carried identity values used when success is structural-only."""
    return {
        key: value
        for key, value in target.items()
        if key not in _STATE_KEYS
        and key.casefold() not in _CONTENT_KEYS
        and isinstance(value, (str, int, float, bool))
    }


def exact_target_evidence(
    statement: StatementContract,
    observation: Observation,
) -> dict[str, Any]:
    """Match target-bound identity against structured evidence on the current UI.

    Identity is value-based:
    1. Prefer keys declared in both ``target`` and expected/postcondition state
       (legacy programs that still copy row fields into ``success``).
    2. If success is structural-only (``entity`` / ``fields``), fall back to the
       primitive identity fields on ``target`` itself (not free-text body/content).
       Detail pages must still exhibit those values; collection membership still
       blocks complete.
    """
    target = statement.inputs.get("target")
    ui_state = statement.inputs.get("ui_state")
    state = statement.expected_state or (
        ui_state.get("postcondition") if isinstance(ui_state, dict) else None
    )
    if not isinstance(target, dict) or not target:
        return {"status": "not_applicable"}
    identity = {}
    if isinstance(state, dict) and state:
        identity = {
            key: value
            for key, value in state.items()
            if key not in _STATE_KEYS and key in target and target[key] == value
        }
    if not identity:
        identity = _primitive_identity(target)
    if not identity:
        return {"status": "not_applicable"}
    return exact_identity_evidence(identity, observation)


__all__ = ["exact_identity_evidence", "exact_target_evidence"]
