"""Validated collection handles produced by query-only lookup statements."""

from __future__ import annotations

from typing import Any

from gui_agent.core.filter_contract import (
    canonical_filter_field,
)
from gui_agent.core.run.collection_view import collection_candidates
from gui_agent.core.schemas import CollectionIntent, Observation


_KIND = "resolved_collection"
_GENERIC_SCOPE_WORDS = {"grid", "list", "page", "table", "view", "workspace"}


def _semantic_key(value: Any) -> str:
    return "".join(
        char for char in canonical_filter_field(value)
        if char.isalnum()
    )


def _semantic_words(value: Any) -> set[str]:
    words = set(canonical_filter_field(value).split())
    return set(words) - _GENERIC_SCOPE_WORDS


def is_lookup_scope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == _KIND
        and bool(value.get("surface_fingerprint"))
    )


def resolve_lookup_scope(
    observation: Observation,
    request: CollectionIntent,
) -> dict[str, Any] | None:
    """Locate one structural collection by identity, without guessing among candidates.

    Lookup is a pure addressing step: it never gates on applied filters. Narrowing
    a collection is the ``constrain`` statement's job.
    """
    candidates = collection_candidates(observation)
    if not candidates:
        return None

    entity = request.entity
    fallback = request.fallback
    field = request.field
    required_fields = request.required_fields

    mention_values = {
        str(value).strip()
        for value in (entity, fallback)
        if str(value or "").strip()
    }
    mentions = {_semantic_key(value) for value in mention_values}
    required = {_semantic_key(field_name) for field_name in required_fields}
    eligible = [
        candidate for candidate in candidates
        if _semantic_key(candidate.get("caption")) in mentions
    ]
    if len(eligible) != 1 and request.coverage == "complete":
        traversable = [
            candidate
            for candidate in candidates
            if candidate.get("projection") == "cells"
            and (candidate.get("traversal") or {}).get("type") in {"scroll", "paged"}
        ]
        if len(traversable) == 1:
            eligible = traversable
    if len(eligible) != 1:
        actionable = [
            candidate
            for candidate in candidates
            if candidate.get("projection") == "cells"
            and any(
                cell.get("controls")
                for cell in candidate.get("table", {}).get("_collection_cells", [])
            )
        ]
        if len(actionable) == 1:
            eligible = actionable
    if len(eligible) != 1 and len(candidates) == 1:
        # A raw-cell sensor establishes the address of the sole collection, but
        # deliberately does not invent its business schema. Acquire validates the
        # requested fields while projecting exact cell sources into records.
        if candidates[0].get("projection") == "cells":
            eligible = candidates
    if len(eligible) != 1 and len(candidates) == 1:
        title_words = _semantic_words(observation.title)
        if title_words and any(
            title_words & _semantic_words(value) for value in mention_values
        ):
            eligible = candidates
    if len(eligible) != 1 and required:
        eligible = [
            candidate for candidate in candidates
            if required <= {
                _semantic_key(header)
                for header in candidate.get("headers") or []
            }
        ]
    if len(eligible) != 1:
        field_key = _semantic_key(field)
        matching = [
            candidate for candidate in candidates
            if field_key in {
                _semantic_key(header) for header in candidate.get("headers") or []
            }
        ]
        eligible = matching
        if len(eligible) != 1:
            return None
    chosen = eligible[0]
    available_fields = [str(value) for value in chosen.get("headers") or []]
    projection = str(chosen.get("projection") or "rows")
    if (
        projection != "cells"
        and not required <= {_semantic_key(field) for field in available_fields}
    ):
        return None
    return {
        "kind": _KIND,
        "entity": entity,
        "surface_fingerprint": str(chosen["surface_fingerprint"]),
        "available_fields": available_fields,
        "projection": projection,
    }
