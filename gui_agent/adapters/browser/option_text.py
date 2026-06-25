"""Shared option-text extraction for native <select> rescues.

Used by both ``executor.py`` (DOM snap rescue, post-click) and ``policies.py``
(pre-action select_option synthesis) — both need "does this instruction/description
intend to set a native select's value, and if so, what value" off the same free-text
shape, so the regex + fallback chain lives here once instead of twice.
"""

from __future__ import annotations

import re

_QUOTED_OPTION_RE = re.compile(r"[「『\"']([^「」『』\"']{1,40})[」』\"']")
_OPTION_AFTER_EQUALS_RE = re.compile(r"(?:=|为|to)\s*([A-Za-z][\w ._/-]{0,39})", re.IGNORECASE)
_OPTION_AFTER_SELECT_RE = re.compile(
    r"(?:选择|选中|select)\s*([A-Za-z][\w ._/-]{0,39})",
    re.IGNORECASE,
)
_SELECT_INTENT_RE = re.compile(
    r"选择|选中|设为|设置|筛选.*=|下拉.*选项|select\s+option",
    re.IGNORECASE,
)


def option_text_from_instruction(text: str) -> str:
    """Option value for a native select rescue, gated by select intent so a plain
    "click Status" tap never tries to set the select value to "Status"."""
    text = (text or "").strip()
    if not text or not _SELECT_INTENT_RE.search(text):
        return ""
    quoted = _QUOTED_OPTION_RE.findall(text)
    if quoted:
        return quoted[-1].strip()
    match = _OPTION_AFTER_EQUALS_RE.search(text)
    if match:
        return match.group(1).strip(" .,;，。；")
    match = _OPTION_AFTER_SELECT_RE.search(text)
    if match:
        value = match.group(1).strip(" .,;，。；")
        return "" if value.lower() in {"option", "options"} else value
    return ""
