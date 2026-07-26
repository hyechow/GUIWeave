"""Adapter from a semantic ``Interact`` invocation to the GUI executor."""

from __future__ import annotations

from gui_agent.core.run.contracts import Interact, StatementInvocation
from gui_agent.core.schemas import StatementContract, StatementInfo

def statement_id(invocation: StatementInvocation, program_index: int) -> str:
    return invocation.id or invocation.bind or f"s{program_index + 1}"


def contract_for_interact(
    invocation: StatementInvocation,
    program_index: int,
) -> StatementContract:
    statement = invocation.statement
    if not isinstance(statement, Interact):
        raise ValueError("only Interact enters the GUI statement executor")
    return StatementContract(
        id=statement_id(invocation, program_index),
        goal=statement.goal,
        success=statement.success,
        interaction_intent=statement.interaction_intent,
        inputs=dict(invocation.inputs),
        required_values=dict(statement.required_values),
        observe_fields=list(statement.observe_fields),
        persistence=statement.persistence,
    )


def statement_info(
    invocation: StatementInvocation,
    program_index: int,
) -> StatementInfo:
    if isinstance(invocation.statement, Interact):
        contract = contract_for_interact(invocation, program_index)
        return StatementInfo(
            id=contract.id,
            executor="interact",
            goal=contract.goal,
            success=contract.success,
            interaction_intent=contract.interaction_intent,
            inputs=dict(contract.inputs),
            required_values=dict(contract.required_values),
            observe_fields=list(contract.observe_fields),
            persistence=contract.persistence,
        )
    return StatementInfo(
        id=statement_id(invocation, program_index),
        executor=invocation.executor,
        goal=invocation.goal,
        success=invocation.goal,
        inputs=dict(invocation.inputs),
        returns=dict(invocation.statement.returns),
    )


def statement_info_from_contract(contract: StatementContract) -> StatementInfo:
    return StatementInfo(
        id=contract.id,
        executor="interact",
        goal=contract.goal,
        success=contract.success,
        interaction_intent=contract.interaction_intent,
        inputs=dict(contract.inputs),
        required_values=dict(contract.required_values),
        observe_fields=list(contract.observe_fields),
        persistence=contract.persistence,
    )


def start_statement(
    supervisor,
    invocation: StatementInvocation,
    index: int,
    *,
    instance_id: str = "",
) -> StatementContract:
    contract = contract_for_interact(invocation, index)
    supervisor.begin_statement(
        contract,
        instance_id=instance_id or f"inst-{index}-{contract.id}",
        task_type="action",
    )
    return contract


__all__ = [
    "contract_for_interact",
    "start_statement",
    "statement_id",
    "statement_info",
    "statement_info_from_contract",
]
