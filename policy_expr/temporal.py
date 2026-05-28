"""Deterministic temporal expression resolution for Chinese text.

Replaces relative time expressions (上周, 本月, 昨天, etc.) with absolute
ISO date ranges (YYYY-MM-DD至YYYY-MM-DD) using Python datetime arithmetic.
No LLM involved — eliminates date calculation errors in the decomposer.
"""

from __future__ import annotations

import re
from datetime import date, timedelta


def resolve_temporal_expressions(text: str, today: date | None = None) -> str:
    """Replace Chinese relative time expressions with absolute date ranges.

    Handles: 上周, 本周, 上上周, 本月, 上月, 昨天, 今天, 前天, 最近N天,
    N月最后一周, 今年.
    Idempotent: text already containing ISO dates is left unchanged.
    """
    if today is None:
        today = date.today()

    # Apply patterns longest-first to avoid "上上周" matching as "上周" first.
    for pattern, handler in _PATTERNS:
        text = pattern.sub(lambda m, h=handler: _fmt(*h(m, today)), text)
    return text


# ── Helpers ────────────────────────────────────────────────────────────────


def _fmt(start: date, end: date) -> str:
    if start == end:
        return start.isoformat()
    return f"{start.isoformat()}至{end.isoformat()}"


def _monday(d: date) -> date:
    """Monday of the week containing *d* (China: Monday = first day)."""
    return d - timedelta(days=d.weekday())


def _sunday(d: date) -> date:
    return _monday(d) + timedelta(days=6)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


# ── Pattern handlers ──────────────────────────────────────────────────────
# Each handler receives (match, today) and returns (start, end).


def _last_week(m: re.Match, today: date) -> tuple[date, date]:
    mon = _monday(today)
    return mon - timedelta(days=7), mon - timedelta(days=1)


def _week_before_last(m: re.Match, today: date) -> tuple[date, date]:
    mon = _monday(today)
    return mon - timedelta(days=14), mon - timedelta(days=8)


def _this_week(m: re.Match, today: date) -> tuple[date, date]:
    return _monday(today), _sunday(today)


def _this_month(m: re.Match, today: date) -> tuple[date, date]:
    return _month_start(today), _month_end(today)


def _last_month(m: re.Match, today: date) -> tuple[date, date]:
    first = today.replace(day=1) - timedelta(days=1)
    return _month_start(first), _month_end(first)


def _yesterday(m: re.Match, today: date) -> tuple[date, date]:
    d = today - timedelta(days=1)
    return d, d


def _today(m: re.Match, today: date) -> tuple[date, date]:
    return today, today


def _day_before_yesterday(m: re.Match, today: date) -> tuple[date, date]:
    d = today - timedelta(days=2)
    return d, d


def _last_n_days(m: re.Match, today: date) -> tuple[date, date]:
    n = int(m.group(1))
    return today - timedelta(days=n - 1), today


def _this_year(m: re.Match, today: date) -> tuple[date, date]:
    return date(today.year, 1, 1), date(today.year, 12, 31)


def _last_week_of_month(m: re.Match, today: date) -> tuple[date, date]:
    """N月最后一周 — the week containing the last day of month N."""
    month = int(m.group(1))
    year = today.year
    last_day = _month_end(date(year, month, 1))
    # The last week starts on the Monday of the week containing last_day
    start = _monday(last_day)
    # If the Monday falls in the previous month, move to the 1st
    if start.month != month:
        start = date(year, month, 1)
    return start, _sunday(last_day)


# ── Pattern table (order matters: longest match first) ────────────────────

_PATTERNS: list[tuple[re.Pattern, object]] = [
    # Multi-char fixed expressions (longer first)
    (re.compile(r"上上周"), _week_before_last),
    (re.compile(r"上个月|上月"), _last_month),
    (re.compile(r"上个?周|上周"), _last_week),
    (re.compile(r"这个?周|本周"), _this_week),
    (re.compile(r"这个?月|本月"), _this_month),
    (re.compile(r"今年"), _this_year),
    # "N月最后一周" — must come before single-day patterns
    (re.compile(r"(\d{1,2})月最后一周"), _last_week_of_month),
    # Single-day expressions
    (re.compile(r"大前天"), lambda m, t: (t - timedelta(days=3), t - timedelta(days=3))),
    (re.compile(r"前天"), _day_before_yesterday),
    (re.compile(r"昨天"), _yesterday),
    (re.compile(r"今天"), _today),
    # "最近N天"
    (re.compile(r"最近(\d{1,2})天"), _last_n_days),
]
