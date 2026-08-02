from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.orchestrator.models import CurrentUI, require_current_ui
from gui_agent.core.orchestrator.sandbox import validate_code
from gui_agent.core.run.target_evidence import exact_target_evidence
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.context_projection import (
    project_transition_observation,
)
from gui_agent.core.supervisor.statement.observation_view import (
    build_observation_view,
)
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAssessment,
    _TransitionEvidence,
)


TARGET = {
    "author_handle": "@pupper",
    "content": "The Labrador Retriever is friendly and outgoing. " * 3,
}


def _statement() -> StatementContract:
    return StatementContract(
        id="target",
        goal="Open the exact target",
        success="Every target identity field is visible",
        expected_state={"entity": "Toot", **TARGET},
        inputs={"target": TARGET},
    )


def _cell_observation(cells: list[dict]) -> Observation:
    return Observation.model_validate({
        "png_bytes": b"frame",
        "source": "android",
        "collection_regions": [{
            "ref": "feed",
            "surface_fingerprint": "feed:v1",
            "cells": [{"ref": f"cell:{index}", **cell} for index, cell in enumerate(cells)],
        }],
    })


def _observation(content: str) -> Observation:
    return _cell_observation([{
                "ref": "author",
                "structural_key": "author",
                "content_key": "author:pupper",
                "texts": ["now · @pupper"],
            }, {
                "ref": "body",
                "structural_key": "body",
                "content_key": "body:visible",
                "texts": [content],
            }])


def _complete() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="satisfied",
            summary="the requested target is visible",
            established_facts=["the requested target is visible"],
            last_action_effect="none",
        ),
        kind="complete",
        reason="the requested target is visible",
        evidence=[_TransitionEvidence(
            source="current_observation",
            claim="the requested target is visible",
        )],
    )


def test_compiler_requires_target_reach_before_targeted_commit() -> None:
    invalid = """
def run(ctx):
    target = {"ID": "1"}
    ctx.commit("Update order", target=target, values={"Status": "Complete"})
"""
    valid = """
def run(ctx):
    target = {"ID": "1"}
    ctx.reach(
        "Open the exact order",
        target=target,
        success={"entity": "Order", "ID": target["ID"]},
    )
    ctx.commit("Update order", target=target, values={"Status": "Complete"})
"""
    incomplete = """
def run(ctx):
    target = {"ID": "1"}
    ctx.reach("Open an order", target=target, success={"entity": "Order"})
    ctx.commit("Update order", target=target, values={"Status": "Complete"})
"""
    aliased = """
def run(ctx):
    target = {"ID": "1"}
    target_id = target["ID"]
    ctx.reach(
        "Open the exact order",
        target=target,
        success={"entity": "Order", "ID": target_id},
    )
    ctx.commit("Update order", target=target, values={"Status": "Complete"})
"""

    assert any(
        item.code == "COMMIT_TARGET_UI_REQUIRED"
        for item in validate_code(invalid)
    )
    assert "do not nest" in next(
        item.message
        for item in validate_code(invalid)
        if item.code == "COMMIT_TARGET_UI_REQUIRED"
    )
    assert any(
        item.code == "TARGET_REACH_IDENTITY_REQUIRED"
        for item in validate_code(incomplete)
    )
    assert "target['<field>']" in next(
        item.message
        for item in validate_code(incomplete)
        if item.code == "TARGET_REACH_IDENTITY_REQUIRED"
    )
    assert validate_code(valid) == []
    assert validate_code(aliased) == []


def test_live_collection_write_replay_requires_loop_local_target_reach() -> None:
    source = """
def run(ctx):
    ctx.reach("Open tagged toots", success={"entity": "TaggedToots"})
    tagged = ctx.query(
        entity="TaggedToots", fields=["author_handle", "content"]
    )
    ctx.reach("Open bookmarks", success={"entity": "SavedBookmarks"})
    bookmarks = ctx.query(
        entity="SavedBookmarks", fields=["author_handle", "content"]
    )
    excluded = {(row["author_handle"], row["content"]) for row in bookmarks}
    pending = [
        row for row in tagged
        if (row["author_handle"], row["content"]) not in excluded
    ]
    ctx.reach("Return to tagged toots", success={"entity": "TaggedToots"})
    for row in pending:
        ctx.commit("Favorite toot", target=row, values={"favorited": True})
"""

    diagnostics = validate_code(source)

    assert any(
        item.code == "COMMIT_TARGET_UI_REQUIRED" for item in diagnostics
    )


def test_current_ui_rejects_a_different_commit_target() -> None:
    state = CurrentUI(token="c1:state", target={"ID": "1"})

    assert require_current_ui(state, target={"ID": "1"}) is state
    try:
        require_current_ui(state, target={"ID": "2"})
    except ValueError as exc:
        assert "not bound" in str(exc)
    else:
        raise AssertionError("mismatched target must be rejected")


def test_exact_target_evidence_preserves_source_text_and_sigils() -> None:
    statement = _statement()
    observation = _observation(TARGET["content"])

    evidence = exact_target_evidence(statement, observation)
    projected = project_transition_observation(
        statement,
        observation,
        build_observation_view(statement, observation, []),
        initial_filters=None,
    )

    assert evidence == {
        "status": "matched",
        "fields": ["author_handle", "content"],
        "missing_fields": [],
    }
    assert projected["target_evidence"] == evidence


def test_mastodon_replay_distinguishes_adjacent_toot_bodies() -> None:
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "evals/android/acquire/mastodon_20260801.json"
        ).read_text(encoding="utf-8")
    )
    target = next(
        row for row in fixture["expected"]["tagged"]
        if row["content"].startswith("The Labrador Retriever")
    )
    statement = StatementContract(
        id="target",
        goal="Open the exact target",
        success="Every target identity field is visible",
        expected_state={"entity": "Toot", **target},
        inputs={"target": target},
    )

    frames = fixture["collections"]["tagged"]
    wrong = next(
        frame for frame in frames
        if any(
            text.startswith("#lovely #pets Bulldogs")
            for cell in frame["cells"] for text in cell.get("texts", [])
        )
        and not any(
            text == target["content"]
            for cell in frame["cells"] for text in cell.get("texts", [])
        )
    )
    matching = next(
        frame for frame in frames
        if any(
            text == target["content"]
            for cell in frame["cells"] for text in cell.get("texts", [])
        )
        and any(
            target["author_handle"] in text.split()
            for cell in frame["cells"] for text in cell.get("texts", [])
        )
    )

    assert exact_target_evidence(statement, _cell_observation(wrong["cells"]))["status"] == "absent"
    assert exact_target_evidence(statement, _cell_observation(matching["cells"]))["status"] == "matched"


def test_target_reach_cannot_complete_on_a_different_structured_record(
    monkeypatch,
) -> None:
    statement = _statement()
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="i1:target")
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _obs: False)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: _complete(),
    )

    step = policy._run_single_turn(
        statement,
        _observation("A different visible toot body."),
        [],
        validation_retries=0,
    )

    assert step.outcome is None
    assert step.retry_transition is True
    assert "content" in step.summary
