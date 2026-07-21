"""Deterministic executor for Program-defined data transformations."""

from __future__ import annotations

from typing import Any, Callable

from pydantic import JsonValue

from gui_agent.core.orchestrator.program import Compute, ComputeRef
from gui_agent.core.orchestrator.runner import StatementInvocation, matches_output_spec
from gui_agent.core.schemas import Observation, StatementOutcome

from .compute_kernel import ComputeKernelError, ComputeStep, execute_pipeline, json_value


def _resolve(value: Any, ref: ComputeRef) -> Any:
    for part in ref.path:
        if isinstance(part, int) and isinstance(value, list):
            try:
                value = value[part]
            except IndexError as exc:
                raise ComputeKernelError(f"compute output ref out of range: {ref.path!r}") from exc
        elif isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ComputeKernelError(f"compute output ref does not exist: {ref.path!r}")
    return value


def _bind_semantic_fields(steps: list[ComputeStep], bindings: dict[str, Any]) -> list[ComputeStep]:
    """Resolve compiler-owned semantic field names before kernel execution."""

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            if result.get("semantic") is True and isinstance(result.get("path"), list):
                path = list(result["path"])
                if not path or not isinstance(path[0], str) or path[0] not in bindings:
                    raise ComputeKernelError(
                        f"semantic field is not bound: {path[0] if path else '<empty>'}"
                    )
                actual = bindings[path[0]]
                if not isinstance(actual, str) or not actual:
                    raise ComputeKernelError(f"semantic field has invalid binding: {path[0]}")
                result["path"] = [actual, *path[1:]]
                result["semantic"] = False
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return [type(step).model_validate(visit(step.model_dump(mode="json"))) for step in steps]


def execute_compute_statement(
    invocation: StatementInvocation,
    *,
    observation: Observation | None,
    say: Callable[[str], None] = lambda _message: None,
    status: Callable[[str], None] = lambda _message: None,
) -> StatementOutcome:
    if not isinstance(invocation.statement, Compute):
        raise TypeError("execute_compute_statement requires a Compute invocation")

    statement = invocation.statement
    try:
        if statement.source not in invocation.inputs:
            raise ComputeKernelError(f"compute source is not an input: {statement.source}")
        bindings = invocation.inputs.get("bindings", {})
        if not isinstance(bindings, dict):
            raise ComputeKernelError("compute bindings input must be a record")
        steps = _bind_semantic_fields(statement.steps, bindings)
        source = invocation.inputs[statement.source]
        descriptor = invocation.input_descriptors.get(statement.source)
        source_incomplete = (
            isinstance(source, dict) and bool(source.get("partial"))
        ) or (descriptor is not None and descriptor.coverage != "complete")
        input_unverified = (
            descriptor is not None and descriptor.verification != "confirmed"
        )
        if (source_incomplete or input_unverified) and any(
            spec.coverage == "complete" for spec in statement.returns.values()
        ):
            raise ComputeKernelError("compute source is partial but output requires complete coverage")
        result, trace = execute_pipeline(source, steps)
        outputs: dict[str, JsonValue] = {
            name: json_value(_resolve(result, ref))
            for name, ref in statement.outputs.items()
        }
        missing = [
            name for name, spec in statement.returns.items()
            if spec.required and name not in outputs
        ]
        invalid = [
            name for name, spec in statement.returns.items()
            if name in outputs and not matches_output_spec(outputs[name], spec)
        ]
        if missing or invalid:
            raise ComputeKernelError(
                f"compute output contract failed: missing={missing}, invalid={invalid}"
            )
    except ComputeKernelError as exc:
        message = str(exc)
        if message.startswith("semantic field"):
            status("Compute 缺少字段绑定，正在请求重编排…")
            return StatementOutcome.infeasible(
                f"Compute 数据源不足：{message}",
                kickback=(
                    "Compute 所需语义字段无法绑定到当前输入。请先用 SourceCheck 判断字段可读性；"
                    "必要时由 Interact 暴露字段或由 Acquire 取得包含该字段的集合。"
                ),
                observation=observation,
                failure_evidence=message,
            )
        status("Compute 数据计算失败")
        return StatementOutcome.exhausted(
            f"Compute 失败：{message}",
            observation=observation,
            failure_evidence=message,
        )

    verification = "accepted_unverified" if input_unverified else "confirmed"
    say(f"  [Compute] 完成：{', '.join(outputs)}")
    status("Compute 数据计算完成")
    return StatementOutcome.completed(
        statement.goal,
        verification=verification,
        outputs=outputs,
        evidence=[f"compute:{item}" for item in trace],
        observation=observation,
    )


__all__ = ["execute_compute_statement"]
