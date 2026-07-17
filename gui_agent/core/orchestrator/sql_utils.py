"""SQLite identifier normalization used by the runtime Data kernel."""

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
