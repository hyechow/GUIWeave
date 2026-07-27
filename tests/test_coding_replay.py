from __future__ import annotations

from pathlib import Path

import pytest

from gui_agent.core.orchestrator.replay import (
    RecordedContext,
    load_recorded_cases,
    replay_program,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "coding_replay"
    / "recent_webarena.json"
)

SOURCES = {
    42: """
def run(ctx):
    state = ctx.reach("Open search terms", success={"entity": "Search Terms"})
    rows = ctx.query(
        state,
        entity="Search Terms",
        fields=["Search Query", "Number of Uses"],
        coverage="complete",
    )
    assert len(rows) >= 2, "at least two search terms are required"
    ranked = sorted(rows, key=lambda row: row["Number of Uses"], reverse=True)
    return [row["Search Query"] for row in ranked[:2]]
""",
    63: """
def run(ctx):
    state = ctx.reach("Open orders", success={"entity": "Orders"})
    rows = ctx.query(
        state,
        entity="Orders",
        fields=["ID", "Status", "Customer Email"],
        filters={"Status": "Complete"},
        coverage="complete",
    )
    counts = {}
    for row in rows:
        email = row["Customer Email"]
        counts[email] = counts.get(email, 0) + 1
    levels = sorted(set(counts.values()), reverse=True)
    assert len(levels) >= 2, "a second order-count level is required"
    return sorted([email for email, count in counts.items() if count == levels[1]])
""",
    108: """
from datetime import datetime

def run(ctx):
    state = ctx.reach("Open orders", success={"entity": "Orders"})
    rows = ctx.query(
        state,
        entity="Orders",
        fields=["Status", "Purchase Date"],
        filters={"Status": "Complete"},
        coverage="complete",
    )
    counts = {month: 0 for month in range(1, 6)}
    for row in rows:
        value = datetime.fromisoformat(row["Purchase Date"])
        if value.year == 2023 and value.month in counts:
            counts[value.month] += 1
    names = ["", "January", "February", "March", "April", "May"]
    return [{"month": names[month], "count": counts[month]} for month in range(1, 6)]
""",
    113: """
def run(ctx):
    state = ctx.reach("Open all reviews", success={"entity": "All Reviews"})
    rows = ctx.query(
        state,
        entity="All Reviews",
        fields=["ID", "Product", "Nickname"],
        filters={"Product": "Olivia zip jacket"},
        coverage="complete",
    )
    if not rows:
        rows = ctx.query(
            state,
            entity="All Reviews",
            fields=["ID", "Product", "Nickname"],
            filters={"Product": "Olivia"},
            coverage="complete",
        )
    nicknames = []
    for row in rows:
        detail = ctx.read(state, target=row, fields=["Detailed Rating"])
        if detail["Detailed Rating"] <= 3:
            nicknames.append(row["Nickname"])
    return nicknames
""",
    193: """
def run(ctx):
    state = ctx.reach("Open orders", success={"entity": "Orders"})
    rows = ctx.query(
        state,
        entity="Orders",
        fields=["Status", "Purchase Date", "Grand Total (Purchased)"],
        filters={"Status": "Complete"},
        coverage="complete",
    )
    assert len(rows) >= 2, "at least two completed orders are required"
    latest = sorted(rows, key=lambda row: row["Purchase Date"], reverse=True)[:2]
    return round(sum(row["Grand Total (Purchased)"] for row in latest), 2)
""",
}


@pytest.mark.parametrize("task_id", [42, 63, 108, 113, 193])
def test_recent_webarena_program_replay(task_id: int) -> None:
    case = load_recorded_cases(FIXTURE)[task_id]
    result = replay_program(SOURCES[task_id], RecordedContext.from_dict(case))

    assert result.ok, result.error
    assert result.return_value == case["expected"]


def test_explicit_short_phrase_branch_returns_matching_product_reviews() -> None:
    case = load_recorded_cases(FIXTURE)[113]
    result = replay_program(SOURCES[113], RecordedContext.from_dict(case))

    queries = [event for event in result.trace if event.op == "query"]
    assert [event.kwargs["filters"] for event in queries] == [
        {"Product": "Olivia zip jacket"},
        {"Product": "Olivia"},
    ]
    assert len(queries[-1].result) == 3
