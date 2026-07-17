from gui_agent.core.schemas import (
    ActionIntent,
    Observation,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement.execution_scope import (
    execution_scope_for,
    history_for_scope,
)


def _contract() -> StatementContract:
    return StatementContract(
        id="edit",
        goal="complete one linear interaction",
        success="the requested result is visible",
    )


def _turn(index: int, instance_id: str) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="browser",
        statement_instance_id=instance_id,
        supervisor=SupervisorStep(
            action_intent=ActionIntent(instruction=f"step {index}"),
            summary="continue",
            statement_id="edit",
            execution_scope=f"{instance_id}/statement",
        ),
    )


def test_invocation_is_the_only_statement_memory_scope_across_pages():
    statement = _contract()
    first = Observation(
        png_bytes=b"png",
        source="browser",
        url="https://example.test/list",
    )
    second = Observation(
        png_bytes=b"png",
        source="browser",
        url="https://example.test/detail/42",
    )
    assert execution_scope_for(statement, first, instance_id="i7:edit") == (
        "i7:edit/statement"
    )
    assert execution_scope_for(statement, second, instance_id="i7:edit") == (
        "i7:edit/statement"
    )


def test_history_is_isolated_by_invocation_not_page_identity():
    history = [_turn(1, "i1:edit"), _turn(2, "i2:edit"), _turn(3, "i1:edit")]
    result = history_for_scope(
        history,
        _contract(),
        Observation(png_bytes=b"png", source="browser"),
        instance_id="i1:edit",
    )
    assert [turn.index for turn in result] == [1, 3]
