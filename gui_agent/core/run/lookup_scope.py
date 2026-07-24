"""Validated collection handles produced by query-only lookup statements."""

from __future__ import annotations

import re
from typing import Any

from gui_agent.core.run.collection_view import collection_candidates
from gui_agent.core.schemas import Observation


_KIND = "resolved_collection"
_GENERIC_SCOPE_WORDS = {"grid", "list", "page", "table", "view", "workspace"}


def _semantic_key(value: Any) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").strip().casefold())


def _semantic_words(value: Any) -> set[str]:
    words = re.findall(r"\w+", str(value or "").casefold())
    return set(words) - _GENERIC_SCOPE_WORDS


def is_lookup_scope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == _KIND
        and bool(value.get("surface_fingerprint"))
    )


def resolve_lookup_scope(
    observation: Observation,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve one structural collection without guessing among candidates."""
    requested_filters = request.get("filters") or {}
    if not isinstance(requested_filters, dict):
        return None
    actual_filters = {
        _semantic_key(name): str(value).strip().casefold()
        for name, value in (observation.applied_filters or {}).items()
    }
    if any(
        actual_filters.get(_semantic_key(name)) != str(value).strip().casefold()
        for name, value in requested_filters.items()
    ):
        return None

    candidates = collection_candidates(observation)
    if not candidates:
        return None

    mention_values = {
        str(value).strip()
        for value in (request.get("entity"), request.get("fallback"))
        if str(value or "").strip()
    }
    mentions = {_semantic_key(value) for value in mention_values}
    eligible = [
        candidate for candidate in candidates
        if _semantic_key(candidate.get("caption")) in mentions
    ]
    if len(eligible) != 1 and len(candidates) == 1:
        title_words = _semantic_words(observation.title)
        if title_words and any(
            title_words & _semantic_words(value) for value in mention_values
        ):
            eligible = candidates
    if len(eligible) != 1:
        filtered = bool(mentions & {
            _semantic_key(value)
            for value in (observation.applied_filters or {}).values()
            if str(value or "").strip()
        })
        field_key = _semantic_key(request.get("field") or "name")
        matching = [
            candidate for candidate in candidates
            if field_key in {
                _semantic_key(header) for header in candidate.get("headers") or []
            }
        ]
        eligible = matching or candidates
        if not filtered or len(eligible) != 1:
            return None
    chosen = eligible[0]
    available_fields = [str(value) for value in chosen.get("headers") or []]
    required = {_semantic_key(field) for field in request.get("required_fields") or []}
    if not required <= {_semantic_key(field) for field in available_fields}:
        return None
    return {
        "kind": _KIND,
        "entity": str(request.get("entity") or ""),
        "filters": dict(requested_filters),
        "surface_fingerprint": str(chosen["surface_fingerprint"]),
        "available_fields": available_fields,
    }
