"""Small runtime helpers shared by the statement supervisor."""

from __future__ import annotations

import time
from llm.structured import get_llm_token_usage
from gui_agent.core.schemas import StatementContract

class _Timer:
    """Context manager: time a named section and optionally record token deltas."""

    def __init__(
        self,
        collector: dict[str, float],
        order: list[str],
        name: str,
        tokens: dict[str, dict[str, int]] | None = None,
    ):
        self._c = collector
        self._o = order
        self._n = name
        self._s = 0.0
        self._tokens = tokens
        self._tok0 = (0, 0)

    def __enter__(self):
        self._s = time.perf_counter()
        if self._tokens is not None:
            self._tok0 = get_llm_token_usage()
        return self

    def __exit__(self, *a):
        d = time.perf_counter() - self._s
        if self._n not in self._c:
            self._o.append(self._n)
        self._c[self._n] = self._c.get(self._n, 0) + d
        if self._tokens is not None:
            inp, out = get_llm_token_usage()
            slot = self._tokens.setdefault(self._n, {"input": 0, "output": 0})
            slot["input"] += inp - self._tok0[0]
            slot["output"] += out - self._tok0[1]


def _ctx(statement: StatementContract, collection_scope=None) -> dict:
    return {
        "statement_id": statement.id,
        "collection_scope": collection_scope,
    }


def _is_home_identity(page_identity: str, markers: tuple[str, ...] = ()) -> bool:
    """Derive "system home/launcher" from platform-provided page_identity markers."""
    if not page_identity or not markers:
        return False
    pid = page_identity.lower()
    return any(marker and marker.lower() in pid for marker in markers)
