import types

from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    PolicyContext,
    PolicyTurn,
    StatementOutcome,
    StatementOutcomeEvent,
    SupervisorStep,
)
from replay.run import (
    _action_expectation_failures,
    _expectation_failures,
    _history_before_selected_event,
    _terminal_event_for_turn,
)


def _turn(index: int) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="browser",
        observation_url=f"screenshot_turn_{index}.png",
        statement_instance_id="i1:c1",
        supervisor=SupervisorStep(summary=f"turn {index}", statement_id="c1"),
    )


def test_terminal_replay_restores_journal_prefix_not_snapshot_number() -> None:
    terminal = StatementOutcomeEvent(
        after_turn=2,
        observation_source="browser",
        observation_url="screenshot_turn_1.png",
        statement_instance_id="i1:c1",
        statement_id="c1",
        outcome=StatementOutcome.failed("budget"),
    )
    context = PolicyContext(
        goal="test",
        supervisor_policy_name="statement",
        action_policy_name="browser_vision",
        journal={"events": [_turn(1), _turn(2), terminal]},
    )

    history = _history_before_selected_event(
        context,
        target_index=1,
        terminal_event=terminal.model_dump(mode="json"),
    )

    assert [event.index for event in history if isinstance(event, PolicyTurn)] == [1, 2]
    assert terminal not in history


def test_terminal_replay_resolves_journal_turn_before_capture_number() -> None:
    raw = {
        "journal": {
            "events": [
                {
                    "event_type": "statement_outcome",
                    "after_turn": 8,
                    "statement_id": "c1",
                    "observation_url": "screenshot_turn_10.png",
                }
            ]
        }
    }

    assert _terminal_event_for_turn(raw, turn=8, statement_id="c1") is not None
    assert _terminal_event_for_turn(raw, turn=10, statement_id="c1") is not None


def test_terminal_expectation_does_not_require_transition_record() -> None:
    decision = SupervisorStep(
        summary="already complete",
        outcome=StatementOutcome.completed("done", verification="confirmed"),
    )
    supervisor = types.SimpleNamespace(_last_transition_record=None)

    assert _expectation_failures(
        {
            "should_act": False,
            "phase": "completed",
            "verification": "confirmed",
        },
        decision,
        supervisor,
    ) == []


def test_action_expectation_can_assert_effective_receipt_role() -> None:
    action = BaseActionDecision(
        action=BaseAction(action_type="tap", x=850, y=40, description="Apply")
    )

    assert _action_expectation_failures(
        {"receipt_role": "commit"},
        action,
        "commit",
    ) == []
