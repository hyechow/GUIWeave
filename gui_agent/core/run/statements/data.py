"""Runtime semantic data executor.

The Program carries only a data goal and typed inputs/outputs.  At execution
time one LLM call proposes a small plan against the real ``DataContextView``;
the restricted data kernel executes it.  A failed plan gets at most one repair.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Union

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.orchestrator.program import Data, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.schemas import Observation, StatementOutcome
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from .data_kernel import (
    DataKernelError,
    DataStep,
    describe_datasets,
    execute_pipeline,
)


_SYSTEM = load_prompt_text("task.statement.data_executor")
_TRACE_LABEL = "statement.data"


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


class TransformOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["transform"] = "transform"
    name: str
    source: DataRef
    steps: list[DataStep] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _validate_steps(self) -> "TransformOp":
        terminal = [index for index, step in enumerate(self.steps) if step.op == "aggregate"]
        if terminal and terminal != [len(self.steps) - 1]:
            raise ValueError("aggregate must be the final transform step")
        return self


class EmitOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["emit"] = "emit"
    values: dict[str, DataRef]


DataOp = Annotated[
    Union[ReadObservationOp, TransformOp, EmitOp],
    Field(discriminator="kind"),
]


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
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _input_context(inputs: dict[str, JsonValue]) -> tuple[dict[str, JsonValue], list[dict[str, Any]]]:
    values: dict[str, JsonValue] = {}
    schemas: list[dict[str, Any]] = []
    for name, value in inputs.items():
        table: dict[str, Any] | None = None
        if isinstance(value, dict) and isinstance(value.get("rows"), list):
            table = dict(value)
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            table = {"rows": value, "partial": False}
        if table is None:
            values[name] = value
            continue
        schema = describe_datasets([table])[0]
        schema.pop("path", None)
        schemas.append({**schema, "source": {"var": name}})
        values[name] = {"record_count": schema["row_count"], "dataset": True}
    return values, schemas


def _context_summary(invocation: StatementInvocation, observation: Observation | None) -> str:
    input_values, input_schemas = _input_context(invocation.inputs)
    payload = {
        "task_goal": invocation.task_goal,
        "goal": invocation.goal,
        "inputs": input_values,
        "returns": {
            name: spec.model_dump(mode="json")
            for name, spec in invocation.statement.returns.items()
        },
        "dataset_schemas": {
            "inputs": input_schemas,
            "observation_tables": describe_datasets(getattr(observation, "tables", None)),
        },
        "observation": {
            "url": getattr(observation, "url", None),
            "title": getattr(observation, "title", None),
            "form_controls": _jsonable(getattr(observation, "form_controls", None)),
        },
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return text[:30000]


def _llm() -> ChatOpenAI:
    cfg = resolve_llm_config("data")
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
        label=_TRACE_LABEL,
        context_reports=context_reports,
        decision_text="生成一次可执行的数据计划。",
    )
    return invoke_structured(
        _llm(),
        messages,
        DataPlan,
        trace_sink=context_reports,
        trace_label=_TRACE_LABEL,
    )


def _resolve(ref: DataRef, bindings: dict[str, Any]) -> Any:
    if ref.var not in bindings:
        raise DataKernelError(f"data ref 未定义: {ref.var}")
    value = bindings[ref.var]
    for part in ref.path:
        if isinstance(part, int) and isinstance(value, list):
            try:
                value = value[part]
            except IndexError as exc:
                raise DataKernelError(f"data ref 越界: {ref.var}.{ref.path}") from exc
        elif isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise DataKernelError(f"data ref 不存在: {ref.var}.{ref.path}")
    return value


def _coerce(value: Any, spec: OutputSpec) -> JsonValue:
    value = _jsonable(value)
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


def _execute(
    plan: DataPlan,
    invocation: StatementInvocation,
    observation: Observation | None,
    *,
    check_knowledge: str,
    prepare_vision_prompt_png,
) -> tuple[dict[str, JsonValue], list[str], bool]:
    bindings: dict[str, Any] = dict(invocation.inputs)
    trace: list[str] = []
    unverified = False
    for operation in plan.operations:
        if isinstance(operation, ReadObservationOp):
            if observation is None:
                raise DataKernelError("当前 Data statement 没有 observation")
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
                unverified = True
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
        if isinstance(operation, TransformOp):
            source = _resolve(operation.source, bindings)
            partial = isinstance(source, dict) and bool(source.get("partial"))
            if partial and any(
                spec.coverage == "complete"
                for spec in invocation.statement.returns.values()
            ):
                raise DataKernelError(
                    "transform source is partial but Data return requires complete coverage"
                )
            if partial and any(
                spec.coverage == "best_effort"
                for spec in invocation.statement.returns.values()
            ):
                unverified = True
            value, step_trace = execute_pipeline(source, operation.steps)
            bindings[operation.name] = value
            trace.extend(f"transform:{operation.name}:{item}" for item in step_trace)
            continue
        values = {
            name: _coerce(_resolve(ref, bindings), invocation.statement.returns[name])
            for name, ref in operation.values.items()
            if name in invocation.statement.returns
        }
        extras = sorted(set(operation.values) - set(invocation.statement.returns))
        if extras:
            raise DataKernelError(f"emit 包含未声明 outputs: {extras}")
        return values, trace, unverified
    raise DataKernelError("data plan did not emit outputs")


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
            outputs, trace, unverified = _execute(
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
                raise DataKernelError(f"emit 缺少必需 outputs: {missing}")
            return StatementOutcome.completed(
                invocation.goal,
                verification="accepted_unverified" if unverified else "confirmed",
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
    "TransformOp",
    "execute_data_statement",
]
