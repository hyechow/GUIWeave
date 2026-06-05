"""Deterministic regression tests for policy_expr.temporal.

These are NOT evals. `resolve_temporal_expressions` is a pure function
(regex + datetime, no LLM), so every case has a single correct output and is
asserted exactly. When a real query exposes a phrasing we mis-handle, add it to
CASES below as a regression — this table is the project's growing date-parsing
corpus.

Run: `uv run pytest tests/test_temporal.py`
"""

from datetime import date

import pytest

from policy_expr.temporal import resolve_temporal_expressions as resolve

# Reference "today" values used across cases.
THU = date(2026, 5, 28)   # a Thursday
JUNE = date(2026, 6, 5)   # the day the date-range corruption was first observed

# Each case: (id, text, today, expected_substrings, forbidden_substrings).
#   expected  — all must appear in the result
#   forbidden — none may appear (e.g. the unresolved word, or a known corruption)
CASES: list[tuple[str, str, date, list[str], list[str]]] = [
    # ── Standalone relative ranges ──────────────────────────────────────────
    ("last_week", "我上周用支付宝支付花多少钱了？", THU, ["2026-05-18至2026-05-24"], ["上周"]),
    ("week_before_last", "上上周的消费记录", THU, ["2026-05-11至2026-05-17"], ["上上周"]),
    ("this_week", "本周花了多少", THU, ["2026-05-25至2026-05-31"], ["本周"]),
    ("this_month", "本月消费", THU, ["2026-05-01至2026-05-31"], ["本月"]),
    ("last_month", "上个月花了多少", THU, ["2026-04-01至2026-04-30"], ["上个月"]),
    ("yesterday", "昨天的订单", THU, ["2026-05-27"], ["昨天"]),
    ("today", "今天的天气", THU, ["2026-05-28"], ["今天"]),
    ("day_before_yesterday", "前天的消息", THU, ["2026-05-26"], ["前天"]),
    ("last_3_days", "最近3天的记录", THU, ["2026-05-26至2026-05-28"], ["最近3天"]),
    ("last_7_days", "最近7天消费", THU, ["2026-05-22至2026-05-28"], ["最近7天"]),
    ("this_year", "今年的总支出", THU, ["2026-01-01至2026-12-31"], ["今年"]),
    ("last_week_of_april", "4月最后一周", THU, ["2026-04-27至2026-05-03"], ["最后一周"]),
    ("last_week_of_february", "2月最后一周", THU, ["2026-02-23至2026-03-01"], []),
    ("march_31_is_tuesday", "3月最后一周", THU, ["2026-03-30至2026-04-05"], []),

    # ── Boundary conditions ─────────────────────────────────────────────────
    ("monday_boundary", "上周", date(2026, 6, 1), ["2026-05-25至2026-05-31"], []),
    ("sunday_boundary", "本周", date(2026, 5, 31), ["2026-05-25至2026-05-31"], []),

    # ── Pass-through / preservation ─────────────────────────────────────────
    ("no_temporal", "在美团中点一杯奶茶", THU, ["在美团中点一杯奶茶"], ["至"]),
    ("idempotent_iso", "查看2026-05-18至2026-05-24的消费", THU, ["查看2026-05-18至2026-05-24的消费"], []),
    ("preserves_surrounding", "帮我查一下上周微信支付花了多少", THU, ["帮我查一下", "微信支付花了多少"], ["上周"]),

    # ── Month-qualified day ranges (regression: log 20260605_205054) ─────────
    # "上个月" was expanded to a full month, dropping "3号到10号" and producing
    # the corruption "2026-05-313". The day qualifier must survive.
    ("last_month_day_range", "我上个月3号到10号用微信支付花多少钱了？", JUNE,
        ["2026-05-03至2026-05-10"], ["2026-05-313", "上个月", "号到"]),
    ("last_month_short_form", "上月3号到10号的账单", JUNE, ["2026-05-03至2026-05-10"], ["上月"]),
    ("this_month_day_range", "本月8号到12号花了多少", JUNE, ["2026-06-08至2026-06-12"], ["本月"]),
    ("this_month_single_day", "这个月15号的消费", JUNE, ["2026-06-15"], ["至", "这个月"]),
    ("explicit_month_day_range", "5月3号到10号的支出", JUNE, ["2026-05-03至2026-05-10"], []),
    ("explicit_cross_month", "5月28号到6月3号", JUNE, ["2026-05-28至2026-06-03"], []),
    ("explicit_month_single_day", "查一下5月20日的订单", JUNE, ["2026-05-20"], []),
    ("day_range_clamps_month_end", "上个月28号到31号", date(2026, 7, 1), ["2026-06-28至2026-06-30"], []),
    ("standalone_month_unaffected", "上个月花了多少", JUNE, ["2026-05-01至2026-05-31"], ["上个月"]),
]


@pytest.mark.parametrize(
    "text,today,expected,forbidden",
    [(c[1], c[2], c[3], c[4]) for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_resolve(text, today, expected, forbidden):
    result = resolve(text, today=today)
    for sub in expected:
        assert sub in result, f"expected {sub!r} in {result!r}"
    for sub in forbidden:
        assert sub not in result, f"{sub!r} should not appear in {result!r}"
