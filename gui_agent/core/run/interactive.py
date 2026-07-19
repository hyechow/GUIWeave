"""Adapter from a semantic ``Interact`` invocation to the GUI executor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable

from pydantic import JsonValue

from gui_agent.core.orchestrator.program import Interact, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.schemas import StatementContract, StatementInfo

if TYPE_CHECKING:
    from gui_agent.core.schemas import Observation


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
        inputs=dict(invocation.inputs),
        required_values=dict(statement.required_values),
        persistence=statement.persistence,
        returns=dict(statement.returns),
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
            inputs=dict(contract.inputs),
            required_values=dict(contract.required_values),
            persistence=contract.persistence,
            returns=dict(contract.returns),
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
        inputs=dict(contract.inputs),
        required_values=dict(contract.required_values),
        persistence=contract.persistence,
        returns=dict(contract.returns),
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


def _coerce_output(value: str, spec: OutputSpec) -> JsonValue:
    text = value.strip()
    if spec.type == "number":
        try:
            number = float(text.replace(",", ""))
            return int(number) if number.is_integer() else number
        except ValueError:
            return text
    if spec.type == "boolean":
        lowered = text.casefold()
        if lowered in {"true", "yes", "1", "是"}:
            return True
        if lowered in {"false", "no", "0", "否"}:
            return False
    return text


def extract_interact_outputs(
    invocation: StatementInvocation,
    observation: "Observation",
    *,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    say: Callable[[str], None] = lambda _s: None,
    only_fields: Iterable[str] | None = None,
) -> dict[str, JsonValue]:
    statement = invocation.statement
    if not isinstance(statement, Interact) or not statement.returns:
        return {}

    selected = {
        name: spec
        for name, spec in statement.returns.items()
        if only_fields is None or name in only_fields
    }
    if not selected:
        return {}
    fields = list(selected)
    read_spec = "\n".join(
        f"{name}: {spec.description}"
        for name, spec in selected.items()
        if spec.description
    )
    from gui_agent.core.orchestrator.primitives.structured_read import (
        read_form_control_returns,
        structured_read,
    )

    structured = read_form_control_returns(
        getattr(observation, "form_controls", None),
        fields,
        read_spec=read_spec,
    )
    missing = [field for field in fields if field not in structured]
    visual = (
        structured_read(
            observation.png_bytes,
            missing,
            read_spec=read_spec,
            check_knowledge=check_knowledge,
            prepare_vision_prompt_png=prepare_vision_prompt_png,
        )
        if missing
        else {}
    )
    raw = {**visual, **structured}
    outputs = {
        name: _coerce_output(str(raw.get(name, "")), spec)
        for name, spec in selected.items()
    }
    say(f"  [Program] Interact outputs {fields} → {outputs}")
    return outputs


__all__ = [
    "contract_for_interact",
    "extract_interact_outputs",
    "start_statement",
    "statement_id",
    "statement_info",
    "statement_info_from_contract",
]
