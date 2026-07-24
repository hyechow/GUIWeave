"""Validated collection handles produced by query-only lookup statements."""

from __future__ import annotations

import re
from typing import Any

from gui_agent.core.run.collection_view import collection_candidates
from gui_agent.core.schemas import Observation


_KIND = "resolved_collection"


def _semantic_key(value: Any) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").strip().casefold())


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
    candidates = collection_candidates(observation)
    if not candidates:
        return None

    mentions = {
        _semantic_key(value)
        for value in (request.get("entity"), request.get("fallback"))
        if str(value or "").strip()
    }
    eligible = [
        candidate for candidate in candidates
        if _semantic_key(candidate.get("caption")) in mentions
    ]
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
    return {
        "kind": _KIND,
        "entity": str(request.get("entity") or ""),
        "surface_fingerprint": str(chosen["surface_fingerprint"]),
        "available_fields": [str(value) for value in chosen.get("headers") or []],
    }
