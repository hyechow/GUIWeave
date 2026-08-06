"""Lock the runtime goal-driven scroll gate for 'select all rows' commits.

The Mattermost add-all-members commit failed because the supervisor selected only
the visible members and committed without scrolling to the list boundary — the
perception layer already marks the list scrollable, so the runtime must use that
signal (not a knowledge fact) and compare the member-row set across scrolls to
decide when the list is exhausted.
"""

from __future__ import annotations

from gui_agent.core.run.statement_runtime import StatementRuntimeState
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.policy import (
    StatementSupervisorPolicy,
    _is_select_all_commit,
    _scrollable_list_row_ids,
)


def _begin(policy: StatementSupervisorPolicy, statement: StatementContract, instance_id: str) -> None:
    policy._statement_rt = StatementRuntimeState(contract=statement, instance_id=instance_id)


def _contract(**values) -> StatementContract:
    return StatementContract(
        id="add",
        goal="Add all members and post welcome message",
        success="all members added and message posted",
        persistence="explicit_commit",
        required_values=values,
    )


def _member_obs(names: list[str]) -> Observation:
    """A scrollable list region (y-span 186..838) with one text row per member."""
    region = {
        "role": "region",
        "key": "add_members.user_list.section_list",
        "scrollable": True,
        "in_viewport": True,
        "point": {"x": 500.0, "y": 512.0},
        "rect": {"x": 500.0, "y": 512.0, "width": 1000.0, "height": 652.0},
    }
    tree = [region]
    for i, name in enumerate(names):
        tree.append({
            "role": "text", "key": name,
            "point": {"x": 173.0, "y": 241.0 + i * 75.0},
            "in_viewport": True,
        })
    # chrome outside the list span must not leak into the row set.
    tree.append({"role": "text", "key": "Add members", "point": {"x": 302.0, "y": 76.0}})
    tree.append({"role": "text", "key": "A", "point": {"x": 519.0, "y": 199.0}})
    tree.append({"role": "text", "key": "alex", "point": {"x": 149.0, "y": 874.0}})
    return Observation(png_bytes=b"", source="android", semantic_tree=tree)


def test_is_select_all_commit_detects_add_all_members() -> None:
    assert _is_select_all_commit(_contract(add_all_members=True))
    assert not _is_select_all_commit(_contract(add_all_members=False))
    assert not _is_select_all_commit(_contract(destination_folder="x"))
    assert not _is_select_all_commit(_contract(name="reading"))


def test_scrollable_list_row_ids_extracts_rows_only() -> None:
    obs = _member_obs(["alex", "arjun", "chen", "harry"])
    rows = _scrollable_list_row_ids(obs)
    # member names inside the list span, not the header ("A") / chrome / chip.
    assert rows == ("alex", "arjun", "chen", "harry")


def test_select_all_gate_forces_scroll_until_boundary() -> None:
    policy = StatementSupervisorPolicy()
    statement = _contract(add_all_members=True, message="hi")
    _begin(policy, statement, "i1:add")

    # First completion attempt with 8 visible rows -> force a scroll.
    obs1 = _member_obs(["alex", "arjun", "chen", "harry", "lina", "mike", "nina", "oliver"])
    step = policy._select_all_scroll_step(statement, obs1, execution_scope="s")
    assert step is not None
    assert step.action_intent is not None
    assert step.action_intent.family == "iterate"
    assert step.action_intent.role == "iterate"

    # A scroll reveals two new members (sofia, +1) -> still not at the boundary.
    obs2 = _member_obs(["lina", "mike", "nina", "oliver", "sofia", "violet"])
    step = policy._select_all_scroll_step(statement, obs2, execution_scope="s")
    assert step is not None
    assert step.action_intent.family == "iterate"

    # Now every visible row was seen on a prior scroll -> boundary, allow commit.
    obs3 = _member_obs(["oliver", "sofia", "violet"])
    step = policy._select_all_scroll_step(statement, obs3, execution_scope="s")
    assert step is None


def test_select_all_gate_ignores_non_select_all_commit() -> None:
    policy = StatementSupervisorPolicy()
    statement = _contract(destination_folder="Documents/paper")
    _begin(policy, statement, "i1:move")
    obs = _member_obs(["a.pdf", "b.pdf"])
    assert policy._select_all_scroll_step(statement, obs, execution_scope="s") is None


def test_select_all_gate_ignores_no_scrollable_list() -> None:
    policy = StatementSupervisorPolicy()
    statement = _contract(add_all_members=True)
    _begin(policy, statement, "i1:add")
    obs = Observation(png_bytes=b"", source="android", semantic_tree=[
        {"role": "text", "key": "alex", "point": {"x": 173.0, "y": 241.0}},
    ])
    assert policy._select_all_scroll_step(statement, obs, execution_scope="s") is None
