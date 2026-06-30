"""Instruction similarity helpers for loop/stuck guards.

The guards should catch repeated attempts against the same target, not repeated
templates applied to different runtime entities. A foreach body naturally emits
instructions like "click SKU A's Edit" and "click SKU B's Edit"; text overlap is
high, but the target entity changed, so it is progress rather than looping.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


_PUNCT_RE = re.compile(r"[，。、；：:!?！？\"'`’‘“”《》【】\[\]{}（）()\s]+")
_QUOTED_RE = re.compile(r"[\"'「『“‘]([^\"'」』”’]{2,80})[\"'」』”’]")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ALNUM_ENTITY_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9._:-]*[A-Za-z])"
    r"(?=[A-Za-z0-9._:-]*[0-9])"
    r"[A-Za-z0-9][A-Za-z0-9._:-]{2,}[A-Za-z0-9]"
)
_NUMBER_RE = re.compile(r"\b\d{2,}\b")
_ROW_TARGET_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.:-]{2,})\s*行")
_USER_TARGET_RE = re.compile(r"(?:用户|用户名|账号)\s*[\"'「『“‘]?([A-Za-z][A-Za-z0-9_.:-]{2,})")


def normalize_instruction(text: str) -> str:
    """Normalize instruction text for coarse string similarity."""
    return _PUNCT_RE.sub("", (text or "").strip().lower())[:120]


def instruction_entities(text: str) -> set[str]:
    """Extract runtime target tokens such as SKUs, IDs, emails, or quoted values."""
    raw = text or ""
    entities: set[str] = set()
    for pattern in (_QUOTED_RE, _EMAIL_RE, _ALNUM_ENTITY_RE, _NUMBER_RE):
        for match in pattern.finditer(raw):
            value = match.group(1) if pattern is _QUOTED_RE else match.group(0)
            value = value.strip().lower()
            if value:
                entities.add(value)
    for pattern in (_ROW_TARGET_RE, _USER_TARGET_RE):
        for match in pattern.finditer(raw):
            value = match.group(1).strip().lower()
            if value:
                entities.add(value)
    return entities


def same_instruction_target(left: str, right: str) -> bool:
    """Return False when both instructions name concrete but different entities."""
    left_entities = instruction_entities(left)
    right_entities = instruction_entities(right)
    if left_entities and right_entities and left_entities != right_entities:
        return False
    return True


def instructions_are_repeated(new: str, old: str, *, threshold: float) -> bool:
    """Similarity predicate used by loop guards.

    Entity mismatch overrides text similarity. This keeps repeated operation
    templates useful in foreach/detail-drill workflows while preserving detection
    for truly repeated attempts against the same target.
    """
    if not same_instruction_target(new, old):
        return False
    n_new = normalize_instruction(new)
    n_old = normalize_instruction(old)
    return bool(n_new and n_old) and SequenceMatcher(None, n_new, n_old).ratio() >= threshold
