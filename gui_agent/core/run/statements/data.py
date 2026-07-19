"""Runtime semantic data executor.

The Program carries only a data goal and typed inputs/outputs.  At execution
time one LLM call proposes a small plan against the real ``DataContextView``;
the restricted data kernel executes it.  A failed plan gets at most one repair.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Callable, Literal, Union

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from gui_agent.context import ContextBlock
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.orchestrator.program import Data, OutputSpec
from gui_agent.core.orchestrator.runner import (
    InputDescriptor,
    StatementInvocation,
    matches_output_spec,
)
from gui_agent.core.schemas import Observation, StatementOutcome
from gui_agent.core.run.observation_materializer import materialize_observation
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from .data_kernel import (
    DataKernelError,
    DataStep,
    describe_datasets,
    execute_pipeline,
)


_SYSTEM = load_prompt_text("task.statement.data_executor")
_INSPECT_SYSTEM = load_prompt_text("task.orchestrator.data_inspector")
_TRACE_LABEL = "statement.data"


class DataSourceUnavailable(DataKernelError):
    """The current observation cannot establish a required business fact."""


class DataRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    var: str
    path: list[str | int] = Field(default_factory=list)


class ReadObservationOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["read_observation"] = "read_observation"
    name: str
    source: Literal["page", "controls", "semantic", "datasets", "visual"]
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

    decision: Literal["execute", "unavailable"]
    reasoning: str = ""
    operations: list[DataOp] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    required_coverage: Literal["current_view", "complete"] = "current_view"

    @model_validator(mode="after")
    def _validate_shape(self) -> "DataPlan":
        if self.decision == "unavailable":
            if not self.reasoning.strip():
                raise ValueError("unavailable data plan requires reasoning")
            if self.operations:
                raise ValueError("unavailable data plan cannot carry operations")
            return self
        if self.missing_fields or self.required_coverage != "current_view":
            raise ValueError("execute data plan cannot carry unavailable fields")
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


class DataInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    bindings: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    reasoning: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "DataInspection":
        if self.available and self.missing_fields:
            raise ValueError("available inspection cannot carry missing_fields")
        if not self.available and not self.missing_fields:
            raise ValueError("unavailable inspection requires missing_fields")
        return self


_DataShape = Literal["scalar", "record", "record_list", "table", "list", "unknown"]


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


def _shape_of(value: Any) -> _DataShape:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return "table"
    if isinstance(value, dict):
        return "record"
    if isinstance(value, list):
        return "record_list" if all(isinstance(row, dict) for row in value) else "list"
    if value is None:
        return "unknown"
    return "scalar"


def _ref_shape(
    ref: DataRef,
    shapes: dict[str, _DataShape],
    *,
    site: str,
) -> _DataShape:
    if ref.var not in shapes:
        raise DataKernelError(f"{site}: data ref 未定义: {ref.var}")
    shape = shapes[ref.var]
    for part in ref.path:
        if shape == "record_list" and not isinstance(part, int):
            raise DataKernelError(
                f"{site}: {ref.var} 是 list[record]，不能取字段 path={ref.path!r}；"
                "输出记录列表请使用 path=[]，需要裁剪或改名时先使用 transform.project"
            )
        if shape in {"record_list", "list"} and isinstance(part, int):
            shape = "record" if shape == "record_list" else "unknown"
            continue
        if shape == "table" and isinstance(part, str):
            shape = "record_list" if part == "rows" else "unknown"
            continue
        if shape == "record" and isinstance(part, str):
            shape = "unknown"
            continue
        if shape == "unknown":
            continue
        raise DataKernelError(
            f"{site}: {ref.var} 是 {shape}，path={ref.path!r} 不合法"
        )
    return shape


def _preflight_data_plan(
    plan: DataPlan,
    invocation: StatementInvocation,
    observation: Observation | None,
) -> None:
    """Type-check plan references against real inputs without executing transforms."""
    shapes = {name: _shape_of(value) for name, value in invocation.inputs.items()}
    normalized = materialize_observation(observation)
    for operation in plan.operations:
        if isinstance(operation, ReadObservationOp):
            if operation.name in shapes:
                raise DataKernelError(f"data binding 重复定义: {operation.name}")
            if operation.source == "visual":
                shapes[operation.name] = "record"
            else:
                source = normalized.source(operation.source)
                value = _resolve(
                    DataRef(var=operation.source, path=operation.path),
                    {operation.source: source},
                )
                shapes[operation.name] = _shape_of(value)
            continue
        if isinstance(operation, TransformOp):
            if operation.name in shapes:
                raise DataKernelError(f"data binding 重复定义: {operation.name}")
            source = _ref_shape(
                operation.source,
                shapes,
                site=f"transform {operation.name!r}",
            )
            if source not in {"record_list", "table"}:
                raise DataKernelError(
                    f"transform {operation.name!r}: source 必须是 list[record] 或 table，"
                    f"实际为 {source}"
                )
            shapes[operation.name] = (
                "record" if operation.steps[-1].op == "aggregate" else "record_list"
            )
            continue

        extras = sorted(set(operation.values) - set(invocation.statement.returns))
        if extras:
            declared = sorted(invocation.statement.returns)
            raise DataKernelError(
                f"emit 包含未声明 outputs: {extras}；合法 outputs 仅为 {declared}"
            )
        for name, ref in operation.values.items():
            spec = invocation.statement.returns[name]
            actual = _ref_shape(
                ref,
                shapes,
                site=f"emit output {name!r} expects {spec.type}",
            )
            expected = {
                "record": "record",
                "list[record]": "record_list",
            }.get(spec.type, "scalar" if spec.type != "json" else None)
            if expected is not None and actual not in {expected, "unknown"}:
                raise DataKernelError(
                    f"emit output {name!r} 类型不符合合同：期望 {spec.type}，"
                    f"ref {ref.var}.{ref.path!r} 实际为 {actual}"
                )


def _is_dataset_input(
    value: JsonValue,
    descriptor: InputDescriptor | None,
) -> bool:
    if isinstance(value, dict):
        return isinstance(value.get("rows"), list)
    return bool(
        isinstance(value, list)
        and (
            descriptor is not None and descriptor.type == "list[record]"
            or bool(value)
        )
        and all(isinstance(item, dict) for item in value)
    )


def _input_context(
    invocation: StatementInvocation,
) -> tuple[dict[str, JsonValue], list[dict[str, Any]]]:
    values: dict[str, JsonValue] = {}
    schemas: list[dict[str, Any]] = []
    for name, value in invocation.inputs.items():
        descriptor = invocation.input_descriptors.get(name)
        table: dict[str, Any] | None = None
        if isinstance(value, dict) and _is_dataset_input(value, descriptor):
            table = dict(value)
        elif isinstance(value, list) and _is_dataset_input(value, descriptor):
            table = {"rows": value, "partial": False}
        if table is None:
            values[name] = value
            continue
        schema = describe_datasets([table])[0]
        schema.pop("path", None)
        source = {
            "kind": "materialized_input",
            "var": name,
            **(
                {
                    "binding": descriptor.source_var,
                    **descriptor.model_dump(mode="json", exclude={"source_var"}),
                }
                if descriptor is not None
                else {}
            ),
        }
        schemas.append({
            **schema,
            "source": source,
            "authoritative": bool(
                descriptor is not None
                and descriptor.type == "list[record]"
                and descriptor.coverage == "complete"
                and descriptor.verification == "confirmed"
            ),
        })
        values[name] = {"record_count": schema["row_count"], "dataset": True}
    return values, schemas


def _context_summary(invocation: StatementInvocation, observation: Observation | None) -> str:
    input_values, input_schemas = _input_context(invocation)
    normalized = materialize_observation(observation)
    has_materialized_dataset = bool(input_schemas)
    observation_context = normalized.as_context()
    if has_materialized_dataset:
        # A Data statement consuming an explicit collection must plan against that binding.
        # The live page is only where execution happens to be now; its partial window is not a
        # competing data source and must not be allowed to downgrade upstream coverage.
        observation_context = {
            "page": observation_context["page"],
            "applied_filters": observation_context["applied_filters"],
            "visual_available": observation_context["visual_available"],
            "datasets": [],
            "controls": [],
            "semantic": [],
        }
    payload = {
        "task_goal": invocation.task_goal,
        "goal": invocation.goal,
        "mode": invocation.statement.mode,
        "required_fields": list(invocation.statement.required_fields),
        "inputs": input_values,
        "returns": {
            name: spec.model_dump(mode="json")
            for name, spec in invocation.statement.returns.items()
        },
        "dataset_schemas": {
            "inputs": input_schemas,
            "observation": (
                []
                if has_materialized_dataset
                else describe_datasets(
                    [dataset.as_table() for dataset in normalized.datasets]
                )
            ),
        },
        "source_authority": (
            "materialized_inputs"
            if has_materialized_dataset
            else "current_observation"
        ),
        "observation": observation_context,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _has_materialized_dataset(invocation: StatementInvocation) -> bool:
    return any(
        _is_dataset_input(value, invocation.input_descriptors.get(name))
        for name, value in invocation.inputs.items()
    )


def _has_confirmed_complete_input(invocation: StatementInvocation) -> bool:
    return any(
        descriptor.type == "list[record]"
        and descriptor.coverage == "complete"
        and descriptor.verification == "confirmed"
        for descriptor in invocation.input_descriptors.values()
    )


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
        None if _has_materialized_dataset(invocation) else observation,
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


def _inspect(
    invocation: StatementInvocation,
    observation: Observation | None,
    *,
    context_reports: list[dict] | None = None,
) -> DataInspection:
    block = ContextBlock(
        id="runtime.data_inspection",
        budget="required",
        source_type="runtime_state",
        source="data_context_view",
        ttl="statement",
        priority=10,
        content="## DataContextView\n" + _context_summary(invocation, observation),
    )
    messages = assemble_messages(
        _INSPECT_SYSTEM,
        observation,
        human_blocks=[block],
        image_resize="none",
        label="statement.data.inspect",
        context_reports=context_reports,
        decision_text="判断所需语义字段当前是否全部可读取。",
    )
    return invoke_structured(
        _llm(),
        messages,
        DataInspection,
        trace_sink=context_reports,
        trace_label="statement.data.inspect",
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
    normalized = materialize_observation(observation)
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
                missing_visual = [
                    field for field in fields if value.get(field) in (None, "")
                ]
                if missing_visual:
                    raise DataSourceUnavailable(
                        "visual source cannot establish fields: "
                        + ", ".join(missing_visual)
                    )
                unverified = True
            else:
                source = normalized.source(operation.source)
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
        }
        return values, trace, unverified
    raise DataKernelError("data plan did not emit outputs")


def execute_data_statement(
    invocation: StatementInvocation,
    *,
    observation: Observation | None,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    context_reports: list[dict] | None = None,
    say: Callable[[str], None] = lambda _message: None,
    status: Callable[[str], None] = lambda _message: None,
) -> StatementOutcome:
    if not isinstance(invocation.statement, Data):
        raise TypeError("execute_data_statement requires a Data invocation")
    if invocation.statement.mode == "inspect":
        try:
            inspected = _inspect(
                invocation,
                observation,
                context_reports=context_reports,
            )
        except Exception as exc:  # noqa: BLE001 - typed failure is a statement failure
            return StatementOutcome.exhausted(
                f"Data schema inspect 失败：{exc}",
                observation=observation,
                context_reports=list(context_reports or []),
            )
        outputs = {
            "available": inspected.available,
            "bindings": dict(inspected.bindings),
            "missing_fields": list(inspected.missing_fields),
        }
        say(
            "  [Data] schema "
            + ("可用" if inspected.available else f"缺少 {inspected.missing_fields}")
        )
        status("Data 数据可用性检查完成")
        return StatementOutcome.completed(
            inspected.reasoning or invocation.goal,
            verification="accepted_unverified",
            outputs=outputs,
            evidence=[f"schema:{name}->{field}" for name, field in inspected.bindings.items()],
            observation=observation,
            context_reports=list(context_reports or []),
        )
    error = ""
    for attempt in range(2):
        try:
            plan = _plan(
                invocation,
                observation,
                previous_error=error,
                context_reports=context_reports,
            )
            steps = " → ".join(op.kind for op in plan.operations) or plan.decision
            say(f"  [Data] 计划 {attempt + 1}/2：{steps}")
            if plan.decision == "unavailable":
                if (
                    plan.required_coverage == "complete"
                    and not plan.missing_fields
                    and _has_confirmed_complete_input(invocation)
                ):
                    raise DataKernelError(
                        "Data plan ignored a confirmed complete materialized input; "
                        "plan against the input binding instead of the current observation"
                    )
                fields = ", ".join(plan.missing_fields) or "目标计算所需字段"
                coverage = "完整集合" if plan.required_coverage == "complete" else "当前视图"
                say(f"  [Data] 数据源不足：{plan.reasoning}")
                status("Data 数据不足，正在请求重编排…")
                return StatementOutcome.infeasible(
                    f"Data 数据源不足：{plan.reasoning}",
                    kickback=(
                        f"当前 Data 缺少 {fields}（需要{coverage}覆盖）。请用 Data inspect + Program If "
                        "决定是否由 Interact 暴露字段；需要跨窗口记录时再用 Acquire 物化 list[record] "
                        "并通过 Data.inputs 传入。"
                    ),
                    observation=observation,
                    context_reports=list(context_reports or []),
                )
            _preflight_data_plan(plan, invocation, observation)
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
            invalid = [
                name
                for name, spec in invocation.statement.returns.items()
                if name in outputs and not matches_output_spec(outputs[name], spec)
            ]
            if invalid:
                raise DataKernelError(f"emit outputs 类型不符合合同: {invalid}")
            say(f"  [Data] 完成：{', '.join(outputs) or '无输出'}")
            status("Data 数据处理完成")
            return StatementOutcome.completed(
                invocation.goal,
                verification="accepted_unverified" if unverified else "confirmed",
                outputs=outputs,
                evidence=trace,
                observation=observation,
                context_reports=list(context_reports or []),
            )
        except DataSourceUnavailable as exc:
            status("Data 当前证据不可读，正在请求重编排…")
            return StatementOutcome.infeasible(
                f"Data 当前来源不可用：{exc}",
                kickback=(
                    "由 Program 安排 Interact 暴露所需事实，再由新的 Data 从终态 observation 重读；"
                    "不得把缺少证据解释成业务条件 false。"
                ),
                observation=observation,
                context_reports=list(context_reports or []),
            )
        except Exception as exc:  # noqa: BLE001 - one bounded semantic plan repair
            error = str(exc)
            say(f"  [Data] 计划 {attempt + 1}/2 失败：{error[:240]}")
            if attempt == 0:
                status("Data 计划执行失败，正在修复一次…")
                continue
    status("Data 数据处理失败")
    return StatementOutcome.exhausted(
        f"Data statement 两次计划均失败：{error}",
        observation=observation,
        failure_evidence=error,
        context_reports=list(context_reports or []),
    )


__all__ = [
    "DataPlan",
    "DataInspection",
    "DataRef",
    "EmitOp",
    "ReadObservationOp",
    "TransformOp",
    "execute_data_statement",
]
