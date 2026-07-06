"""Shared SQL micro-utilities for the orchestrator.

`sql_identifier` normalizes a UI display label into a SQLite-safe snake_case
identifier. It is a pure string function with no validation logic, so it lives in
this neutral leaf module rather than under the validator or decomposer subpackages:
the lint layer (validator_*), the frontend (decomposer_*), and the runtime
(data_query) all import it from here. A single definition keeps the validator's
notion of a valid identifier from drifting away from what SQLite actually accepts
— which was the risk when validator_sql.sql_identifier and data_query._identifier
were two independent byte-identical copies.
"""

from __future__ import annotations

import re


def sql_identifier(value: object) -> str:
    """Normalize a display label to a SQLite-safe identifier.

    Lowercase, collapse non-alphanumerics to ``_``, strip leading/trailing
    underscores, and prefix a leading digit with ``c_``. Empty input → "".
    """
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text
