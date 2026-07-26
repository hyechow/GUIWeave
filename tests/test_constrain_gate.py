import pytest

from gui_agent.core.filter_contract import compile_filter_predicates
from gui_agent.core.schemas import CollectionIntent, StatementContract
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def _authorize(phase: str | None, effect: str) -> str:
    intent = None
    if phase:
        intent = CollectionIntent(
            phase=phase,
            entity="Orders",
            required_fields=["Status"] if phase != "constrain" else [],
            predicates=(
                compile_filter_predicates({"Status": "Complete"})
                if phase == "constrain"
                else {}
            ),
        )
    policy = StatementSupervisorPolicy()
    policy.begin_statement(
        StatementContract(
            id="c1",
            goal="establish collection state",
            success="typed postcondition holds",
            interaction_intent=intent,
        ),
        instance_id="i1:c1",
    )
    return policy.authorize_grounded_action(effect)


@pytest.mark.parametrize("phase", ["locate", "constrain"])
@pytest.mark.parametrize(
    ("effect", "allowed"),
    [
        ("query_control", True),
        ("presentation", True),
        ("viewport", True),
        ("pagination", False),
        ("navigation", False),
        ("business_commit", False),
        ("unknown", False),
    ],
)
def test_query_phase_effect_boundary(phase, effect, allowed) -> None:
    assert (_authorize(phase, effect) == "") is allowed


def test_reach_and_general_interaction_respect_phase_ownership() -> None:
    assert _authorize("reach", "navigation") == ""
    assert _authorize("reach", "pagination")
    assert _authorize(None, "pagination")
    assert _authorize(None, "unknown") == ""
