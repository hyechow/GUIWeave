"""Runtime semantic data executor.

The Program carries only a data goal and typed inputs/outputs.  At execution
time one LLM call proposes a small plan against the real ``DataContextView``;
the restricted data kernel executes it.  A failed plan gets at most one repair.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.orchestrator.primitives.data_query import (
    DataQueryError,
    execute_data_query,
)
from gui_agent.core.orchestrator.program import Data, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.schemas import Observation, StatementOutcome
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured


_SYSTEM = load_prompt_text("task.orchestrator.data_executor")


class DataRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    var: str
    path: list[str | int] = Field(default_factory=list)


class ReadObservationOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["read_observation"] = "read_observation"
    name: str
    source: Literal["tables", "form_controls", "url", "title", "visual"]
    path: list[str | int] = Field(
        default_factory=list,
        description="non-visual source only: structural path projected from the source",
    )
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="visual source only: output field -> evidence/read description",
    )

    @model_validator(mode="after")
    def _validate_projection(self) -> "ReadObservationOp":
        if self.source == "visual":
            if not self.fields:
                raise ValueError("visual read requires fields")
            if self.path:
                raise ValueError("visual read cannot carry a structural path")
        elif self.fields:
            raise ValueError("non-visual read cannot carry fields; use path for structural projection")
        return self


class SqlOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sql"] = "sql"
    name: str
    source: str
    sql: str
    returns: list[str]


class EmitOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["emit"] = "emit"
    values: dict[str, DataRef]


DataOp = Annotated[Union[ReadObservationOp, SqlOp, EmitOp], Field(discriminator="kind")]


class DataPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reasoning: str = ""
    operations: list[DataOp]

    @model_validator(mode="after")
    def _validate_shape(self) -> "DataPlan":
        if not self.operations or len(self.operations) > 6:
            raise ValueError("data plan requires 1..6 operations")
        if not isinstance(self.operations[-1], EmitOp):
            raise ValueError("data plan must end with emit")
        if sum(isinstance(op, EmitOp) for op in self.operations) != 1:
            raise ValueError("data plan requires exactly one emit")
        names = [op.name for op in self.operations if hasattr(op, "name")]
        if len(names) != len(set(names)):
            raise ValueError("data operation names must be unique")
        return self


def _jsonable(value: Any) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _context_summary(invocation: StatementInvocation, observation: Observation | None) -> str:
    payload = {
        "task_goal": invocation.task_goal,
        "goal": invocation.goal,
        "inputs": invocation.inputs,
        "returns": {
            name: spec.model_dump(mode="json")
            for name, spec in invocation.statement.returns.items()
        },
        "observation": {
            "url": getattr(observation, "url", None),
            "title": getattr(observation, "title", None),
            "tables": _jsonable(getattr(observation, "tables", None)),
            "form_controls": _jsonable(getattr(observation, "form_controls", None)),
        },
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text[:30000]


def _llm() -> ChatOpenAI:
    cfg = resolve_llm_config("supervisor.data")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    from llm.provider_config import dashscope_extra_body

    return ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        extra_body=dashscope_extra_body(cfg.model),
    )


def _plan(
    invocation: StatementInvocation,
    observation: Observation | None,
    *,
    previous_error: str = "",
    context_reports: list[dict] | None = None,
) -> DataPlan:
    blocks = [
        ContextBlock(
            id="runtime.data_context",
            budget="required",
            source_type="runtime_state",
            source="data_context_view",
            ttl="statement",
            priority=10,
            content="## DataContextView\n" + _context_summary(invocation, observation),
        )
    ]
    if previous_error:
        blocks.append(
            ContextBlock(
                id="runtime.data_plan_error",
                budget="required",
                source_type="runtime_state",
                source="data_executor",
                ttl="turn",
                priority=5,
                content="## Prior plan execution error\n" + previous_error,
            )
        )
    messages = assemble_messages(
        _SYSTEM,
        observation,
        human_blocks=blocks,
        image_resize="none",
        label="orchestrator.data",
        context_reports=context_reports,
        decision_text="生成一次可执行的数据计划。",
    )
    return invoke_structured(
        _llm(),
        messages,
        DataPlan,
        trace_sink=context_reports,
        trace_label="orchestrator.data",
    )


def _resolve(ref: DataRef, bindings: dict[str, JsonValue]) -> JsonValue:
    if ref.var not in bindings:
        raise DataQueryError(f"data ref 未定义: {ref.var}")
    value = bindings[ref.var]
    for part in ref.path:
        if isinstance(part, int) and isinstance(value, list):
            try:
                value = value[part]
            except IndexError as exc:
                raise DataQueryError(f"data ref 越界: {ref.var}.{ref.path}") from exc
        elif isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise DataQueryError(f"data ref 不存在: {ref.var}.{ref.path}")
    return value


def _table_snapshots(value: JsonValue, name: str) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return [dict(value)]
    if isinstance(value, list) and value and all(
        isinstance(item, dict) and "rows" in item for item in value
    ):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        rows = [dict(item) for item in value if isinstance(item, dict)]
        return [{"caption": name, "rows": rows, "partial": False}]
    if isinstance(value, dict):
        return [{"caption": name, "rows": [dict(value)], "partial": False}]
    raise DataQueryError(f"sql source {name!r} 不是 record/list[record]/tables")


def _coerce(value: JsonValue, spec: OutputSpec) -> JsonValue:
    if spec.type == "number" and isinstance(value, str):
        try:
            parsed = float(value.replace(",", ""))
            return int(parsed) if parsed.is_integer() else parsed
        except ValueError:
            return value
    if spec.type == "boolean" and isinstance(value, str):
        if value.casefold() in {"true", "yes", "1", "是"}:
            return True
        if value.casefold() in {"false", "no", "0", "否"}:
            return False
    return value


def _derive_require_complete(invocation: StatementInvocation, operation: SqlOp) -> bool:
    return not any(
        (spec := invocation.statement.returns.get(name)) is not None
        and spec.coverage == "best_effort"
        for name in operation.returns
    )


def _execute(
    plan: DataPlan,
    invocation: StatementInvocation,
    observation: Observation | None,
    *,
    check_knowledge: str,
    prepare_vision_prompt_png,
) -> tuple[dict[str, JsonValue], list[str], bool]:
    bindings: dict[str, JsonValue] = dict(invocation.inputs)
    trace: list[str] = []
    visual_used = False
    for operation in plan.operations:
        if isinstance(operation, ReadObservationOp):
            if observation is None:
                raise DataQueryError("当前 Data statement 没有 observation")
            if operation.source == "visual":
                from gui_agent.core.orchestrator.primitives.structured_read import structured_read

                fields = list(operation.fields)
                value = structured_read(
                    observation.png_bytes,
                    fields,
                    read_spec="\n".join(
                        f"{name}: {description}"
                        for name, description in operation.fields.items()
                    ),
                    check_knowledge=check_knowledge,
                    prepare_vision_prompt_png=prepare_vision_prompt_png,
                )
                visual_used = True
            else:
                empty: JsonValue = [] if operation.source in {"tables", "form_controls"} else ""
                source = _jsonable(getattr(observation, operation.source, None) or empty)
                value = _resolve(
                    DataRef(var=operation.source, path=operation.path),
                    {operation.source: source},
                )
            bindings[operation.name] = _jsonable(value)
            path = f"{operation.path}" if operation.path else ""
            trace.append(f"read_observation:{operation.source}{path}->{operation.name}")
            continue
        if isinstance(operation, SqlOp):
            if operation.source not in bindings:
                raise DataQueryError(f"sql source 未定义: {operation.source}")
            rows = execute_data_query(
                _table_snapshots(bindings[operation.source], operation.source),
                operation.sql,
                operation.returns,
                require_complete=_derive_require_complete(invocation, operation),
            )
            bindings[operation.name] = _jsonable(rows)
            trace.append(f"sql:{operation.name}:{operation.sql}")
            continue
        values = {
            name: _coerce(_resolve(ref, bindings), invocation.statement.returns[name])
            for name, ref in operation.values.items()
            if name in invocation.statement.returns
        }
        extras = sorted(set(operation.values) - set(invocation.statement.returns))
        if extras:
            raise DataQueryError(f"emit 包含未声明 outputs: {extras}")
        return values, trace, visual_used
    raise DataQueryError("data plan did not emit outputs")


def execute_data_statement(
    invocation: StatementInvocation,
    *,
    observation: Observation | None,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    context_reports: list[dict] | None = None,
) -> StatementOutcome:
    if not isinstance(invocation.statement, Data):
        raise TypeError("execute_data_statement requires a Data invocation")
    error = ""
    for attempt in range(2):
        try:
            plan = _plan(
                invocation,
                observation,
                previous_error=error,
                context_reports=context_reports,
            )
            outputs, trace, visual_used = _execute(
                plan,
                invocation,
                observation,
                check_knowledge=check_knowledge,
                prepare_vision_prompt_png=prepare_vision_prompt_png,
            )
            missing = [
                name
                for name, spec in invocation.statement.returns.items()
                if spec.required and name not in outputs
            ]
            if missing:
                raise DataQueryError(f"emit 缺少必需 outputs: {missing}")
            return StatementOutcome.completed(
                invocation.goal,
                verification="accepted_unverified" if visual_used else "confirmed",
                outputs=outputs,
                evidence=trace,
                observation=observation,
                context_reports=list(context_reports or []),
            )
        except Exception as exc:  # noqa: BLE001 - one bounded semantic plan repair
            error = str(exc)
            if attempt == 0:
                continue
    return StatementOutcome.exhausted(
        f"Data statement 两次计划均失败：{error}",
        observation=observation,
        failure_evidence=error,
        context_reports=list(context_reports or []),
    )


__all__ = [
    "DataPlan",
    "DataRef",
    "EmitOp",
    "ReadObservationOp",
    "SqlOp",
    "execute_data_statement",
]
