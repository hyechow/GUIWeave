from __future__ import annotations

import json

import pytest

from gui_agent.core.run.context import (
    load_observation_snapshot,
    save_observation_snapshot,
)
from gui_agent.core.schemas import Observation
from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from replay.run import (
    _action_expectation_failures,
    _statement_for_terminal_observation,
    _terminal_event_for_observation,
)


def test_observation_snapshot_round_trips_structured_signals_and_adjacent_png(tmp_path):
    screenshot = tmp_path / "screenshot_turn_7.png"
    screenshot.write_bytes(b"real-png-frame")
    source = Observation(
        png_bytes=b"real-png-frame",
        source="browser",
        url="https://example.test/editor",
        title="Editor",
        dom_state="form-v3",
        form_controls=[{"label": "Save", "kind": "button", "ref": 42}],
        form_controls_meta={"coverage": "complete", "returned": 1},
        applied_filters={"Status": "Pending"},
        semantic_tree=[{"role": "button", "key": "Save", "ref": 42, "depth": 1}],
    )
    snapshot = tmp_path / "observation_turn_7.json"

    save_observation_snapshot(snapshot, source, screenshot=screenshot.name)
    restored = load_observation_snapshot(snapshot)

    assert restored == source
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["screenshot"] == screenshot.name
    assert "png_bytes" not in payload["observation"]


def test_observation_snapshot_refuses_missing_screenshot(tmp_path):
    snapshot = tmp_path / "observation_turn_3.json"
    snapshot.write_text(
        json.dumps(
            {
                "version": 1,
                "screenshot": "screenshot_turn_3.png",
                "observation": {"source": "browser"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="replay screenshot not found"):
        load_observation_snapshot(snapshot)


def test_action_replay_expectation_checks_primitive_and_target_region():
    decision = BrowserActionDecision(
        action=BrowserAction(action_type="tap", x=908, y=39, description="commit")
    )
    expectation = {
        "action": {
            "action_type": "tap",
            "x_range": [850, 945],
            "y_range": [0, 100],
        }
    }

    assert _action_expectation_failures(expectation, decision) == []
    assert _action_expectation_failures(
        {"action": {**expectation["action"], "x_range": [950, 1000]}},
        decision,
    ) == ["expected x in [950, 1000], got 908.0"]


def test_terminal_observation_replay_recovers_statement_from_program():
    raw = {
        "orchestrator": {"program": {
            "goal": "persist the edited resource",
            "statements": [{
                "op": "run",
                "statement_id": "s3",
                "name": "ensure values exist",
                "kind": "action",
                "success_condition": "values exist and are saved",
                "persistence": "explicit_commit",
                "target_values": {"Option": ["30", "31"]},
            }],
        }},
        "journal": {
            "events": [
                {
                    "event_type": "statement_outcome",
                    "after_turn": 7,
                    "observation_url": "screenshot_turn_8.png",
                    "statement_instance_id": "i3:s3",
                    "statement_id": "s3",
                }
            ]
        },
    }

    event = _terminal_event_for_observation(raw, turn=8)
    statement = _statement_for_terminal_observation(raw, statement_id="s3")

    assert event is not None
    assert event["statement_instance_id"] == "i3:s3"
    assert statement.id == "s3"
    assert statement.persistence == "explicit_commit"
    assert statement.target_values == {"Option": ["30", "31"]}


def test_terminal_observation_replay_does_not_match_another_screenshot():
    raw = {
        "journal": {
            "events": [
                {
                    "event_type": "statement_outcome",
                    "after_turn": 7,
                    "observation_url": "screenshot_turn_9.png",
                    "statement_id": "other",
                }
            ]
        }
    }

    assert _terminal_event_for_observation(raw, turn=8) is None
