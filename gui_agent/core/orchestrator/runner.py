"""Steppable interpreter for the semantic Program IR.

Only world-facing nodes cross an executor boundary. The
interpreter owns all explicit branching, deterministic iteration and typed
value binding.  It never collects UI rows, writes SQL or asks an LLM to expand
the Program at runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, JsonValue

from gui_agent.core.schemas import Coverage, OutputType, StatementOutcome, Verification

from .program import (
    Acquire,
    Command,
    Compute,
    Condition,
    ExecutableStatement,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    Read,
    SourceCheck,
    StatementNode,
    Stmt,
    ValueRef,
)


ExecutorKind = Literal[
    "interact", "acquire", "read", "source_check", "compute", "command", "program"
]
StatementExecutor = Callable[["StatementInvocation"], StatementOutcome]


class InputDescriptor(BaseModel):
    """Control-plane description of one resolved input; never carries its value."""

    model_config = {"frozen": True, "extra": "forbid"}

    source_var: str
    producer: ExecutorKind = "program"
    output_name: str = ""
    type: OutputType | None = None
    coverage: Coverage = "current_view"
    verification: Verification = "confirmed"


class StatementInvocation(BaseModel):
    """One resolved executor call yielded by the Program interpreter."""

    statement: ExecutableStatement = Field(discriminator="op")
    task_goal: str = ""
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    input_descriptors: dict[str, InputDescriptor] = Field(default_factory=dict)
    args: dict[str, JsonValue] = Field(default_factory=dict)
    loop_path: list[int] = Field(default_factory=list)

    @property
    def id(self) -> str:
        return self.statement.id

    @property
    def bind(self) -> str | None:
        return self.statement.bind

    @property
    def goal(self) -> str:
        return self.statement.goal_text

    @property
    def executor(self) -> Literal[
        "interact", "acquire", "read", "source_check", "compute", "command"
    ]:
        return self.statement.op


class RunRecord(BaseModel):
    """Report/replay projection for one executor invocation or Program failure."""

    node_id: str = ""
    executor: ExecutorKind
    name: str
    var: str | None = None
    result: StatementOutcome
    loop_path: list[int] = Field(default_factory=list)
    instance_id: str = ""
    # Coding-orchestrator only: which ctx.* op produced this statement, plus a
    # report-safe projection of its payload (inputs/filters/fields/…).
    coding_op: str = ""
    coding_payload: dict[str, Any] = Field(default_factory=dict)
    # Plan-level API that expanded into this statement (e.g. query → lookup+acquire).
    coding_plan: str = ""
    coding_plan_step: int = 0
    coding_plan_steps: int = 0


def _flatten_statements(statements: list[Stmt]) -> list[StatementNode]:
    out: list[StatementNode] = []
    for statement in statements:
        if isinstance(statement, StatementNode):
            out.append(statement)
        elif isinstance(statement, If):
            out.extend(_flatten_statements(statement.then))
            out.extend(_flatten_statements(statement.otherwise))
        elif isinstance(statement, ForEach):
            out.extend(_flatten_statements(statement.body))
    return out


def summarize_progress(
    program: Program,
    run_log: list[RunRecord],
    current_run: StatementInvocation | None = None,
) -> tuple[str, str]:
    """Describe completed facts and remaining semantic work for hot recompile."""

    completed_ids = {record.node_id for record in run_log if record.result.is_completed}
    nodes = {node.id: node for node in _flatten_statements(program.statements)}

    def summarize_value(value: JsonValue) -> str:
        if isinstance(value, list):
            if all(isinstance(item, dict) for item in value):
                fields = list(dict.fromkeys(key for row in value for key in row))
                return f"list[record](count={len(value)}, fields={fields})"
            if len(value) <= 10:
                return repr(value)[:240]
            return f"list(count={len(value)})"
        if isinstance(value, dict):
            rows = value.get("rows")
            if isinstance(rows, list) and all(isinstance(item, dict) for item in rows):
                fields = list(dict.fromkeys(key for row in rows for key in row))
                return f"dataset(count={len(rows)}, fields={fields})"
            rendered = repr(value)
            return rendered if len(rendered) <= 240 else f"record(fields={list(value)})"
        return repr(value)[:240]

    experience_lines: list[str] = []
    for record in run_log:
        node = nodes.get(record.node_id)
        outputs: list[str] = []
        for name, value in record.result.outputs.items():
            spec = node.returns.get(name) if node is not None else None
            contract = (
                f", type={spec.type}, coverage={spec.coverage}" if spec is not None else ""
            )
            outputs.append(f"{name}={summarize_value(value)}{contract}")
        binding = f" bind={record.var}" if record.var else ""
        output_text = f" outputs=[{'; '.join(outputs)}]" if outputs else ""
        experience_lines.append(
            f"{'✓' if record.result.is_completed else '✗'} [{record.executor}] "
            f"{record.name}（{record.result.summary}）{binding}{output_text}"
        )
    experience = "\n".join(experience_lines)
    remaining: list[str] = []
    if current_run is not None:
        remaining.append(f"1. [{current_run.executor}] {current_run.goal}")
    current_id = current_run.id if current_run else ""
    for statement in _flatten_statements(program.statements):
        if statement.id in completed_ids or statement.id == current_id:
            continue
        remaining.append(
            f"{len(remaining) + 1}. [{statement.op}] {statement.goal_text}"
        )
    return experience, "\n".join(remaining)


class OrchestratorResult(BaseModel):
    reply: str
    failed: bool = False
    finish_incomplete: bool = False
    env: dict[str, JsonValue] = Field(default_factory=dict)
    run_log: list[RunRecord] = Field(default_factory=list)


def matches_output_spec(value: JsonValue, spec: OutputSpec) -> bool:
    if value is None:
        return not spec.required
    if spec.type in {"text", "url"}:
        return isinstance(value, str) and (bool(value.strip()) or not spec.required)
    if spec.type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec.type == "boolean":
        return isinstance(value, bool)
    if spec.type == "record":
        return isinstance(value, dict) and all(field in value for field in spec.fields)
    if spec.type == "list[record]":
        return isinstance(value, list) and all(
            isinstance(row, dict) and all(field in row for field in spec.fields)
            for row in value
        )
    return True


class Interpreter:
    """Program state and deterministic control-flow owner."""

    def __init__(
        self,
        program: Program,
        *,
        inherited_contracts: Mapping[str, Mapping[str, OutputSpec]] | None = None,
    ) -> None:
        self._program = program
        self._binding_producers = {
            node.bind: node
            for node in _flatten_statements(program.statements)
            if node.bind
        }
        self.binding_contracts: dict[str, dict[str, OutputSpec]] = {
            name: dict(outputs)
            for name, outputs in (inherited_contracts or {}).items()
        }
        self.binding_contracts.update({
            name: dict(node.returns)
            for name, node in self._binding_producers.items()
        })
        self.env: dict[str, JsonValue] = {}
        self.run_log: list[RunRecord] = []
        self.finish_incomplete = False
        self.finish_verification: Verification | None = None
        self.binding_verifications: dict[str, Verification] = {}
        self._frame_verifications: dict[int, dict[str, Verification]] = {}
        self.control_error = ""

    @property
    def failed(self) -> bool:
        return bool(self.control_error) or any(
            not record.result.is_completed for record in self.run_log
        )

    @property
    def terminal_verification(self) -> Verification | None:
        if self.failed:
            return None
        return self.finish_verification or self._combined_verification(
            record.result.verification
            for record in self.run_log
            if record.result.verification is not None
        )

    def steps(self) -> Generator[StatementInvocation, StatementOutcome, str]:
        reply = yield from self._block(self._program.statements, frames=[], loop_path=[])
        return reply if reply is not None else self._auto_summary()

    def _block(
        self,
        statements: list[Stmt],
        *,
        frames: list[dict[str, JsonValue]],
        loop_path: list[int],
    ) -> Generator[StatementInvocation, StatementOutcome, str | None]:
        for statement in statements:
            if isinstance(statement, (Interact, Acquire, Read, SourceCheck, Compute, Command)):
                invocation, error = self._invocation(statement, frames, loop_path)
                if error:
                    outcome = StatementOutcome.failed(error)
                else:
                    outcome = yield invocation
                    outcome = self._propagate_input_verification(statement, outcome, frames)
                    outcome = self._validated_outcome(statement, outcome)
                self.run_log.append(
                    RunRecord(
                        node_id=statement.id,
                        executor=statement.op,
                        name=statement.goal_text,
                        var=statement.bind,
                        result=outcome,
                        loop_path=list(loop_path),
                    )
                )
                if not outcome.is_completed:
                    return f"子任务「{statement.goal_text}」未完成：{outcome.summary}"
                if statement.bind:
                    self._bind(
                        statement.bind,
                        dict(outcome.outputs),
                        frames,
                        verification=outcome.verification or "confirmed",
                    )
                continue
            if isinstance(statement, If):
                branch = statement.then if self._condition(statement.cond, frames) else statement.otherwise
                reply = yield from self._block(branch, frames=frames, loop_path=loop_path)
                if reply is not None:
                    return reply
                continue
            if isinstance(statement, ForEach):
                reply = yield from self._foreach(statement, frames, loop_path)
                if reply is not None:
                    return reply
                continue
            if isinstance(statement, Finish):
                values, missing = self._resolve_map(statement.outputs, frames)
                if missing:
                    self.finish_incomplete = True
                    return self._fail_control(f"最终结果缺少引用：{', '.join(missing)}")
                if statement.outputs:
                    self.finish_verification = self._combined_verification(
                        self._verification(ref, frames)
                        for ref in statement.outputs.values()
                    )
                return self._render_message(statement.message, values)
        return None

    def _invocation(
        self,
        statement: ExecutableStatement,
        frames: list[dict[str, JsonValue]],
        loop_path: list[int],
    ) -> tuple[StatementInvocation, str]:
        inputs, missing = self._resolve_map(statement.inputs, frames)
        if missing:
            return StatementInvocation(statement=statement), (
                f"输入引用不存在：{', '.join(missing)}"
            )
        args: dict[str, JsonValue] = {}
        if isinstance(statement, Acquire) and statement.source_check is not None:
            resolved, ok = self._resolve(statement.source_check, frames)
            if not ok:
                return StatementInvocation(statement=statement), "采集入口检查引用不存在"
            args["source_check"] = resolved
        if isinstance(statement, Command):
            args.update(statement.args)
            for name, ref in statement.arg_refs.items():
                resolved, ok = self._resolve(ref, frames)
                if not ok:
                    return StatementInvocation(statement=statement), f"命令参数引用不存在：{name}"
                args[name] = resolved
        return StatementInvocation(
            statement=statement,
            task_goal=self._program.goal,
            inputs=inputs,
            input_descriptors={
                name: self._input_descriptor(ref, frames)
                for name, ref in statement.inputs.items()
            },
            args=args,
            loop_path=list(loop_path),
        ), ""

    def _input_descriptor(
        self,
        ref: ValueRef,
        frames: list[dict[str, JsonValue]],
    ) -> InputDescriptor:
        producer = self._binding_producers.get(ref.var)
        output_name = ref.path[0] if ref.path and isinstance(ref.path[0], str) else ""
        contracts = self.binding_contracts.get(ref.var, {})
        spec = contracts.get(output_name) if output_name else None
        if not output_name and len(contracts) == 1:
            output_name, spec = next(iter(contracts.items()))
        return InputDescriptor(
            source_var=ref.var,
            producer=producer.op if producer is not None else "program",
            output_name=output_name,
            type=spec.type if spec is not None else None,
            coverage=spec.coverage if spec is not None else "current_view",
            verification=self._verification(ref, frames),
        )

    def _validated_outcome(
        self,
        statement: ExecutableStatement,
        outcome: StatementOutcome,
    ) -> StatementOutcome:
        if not outcome.is_completed:
            return outcome
        invalid = [
            name
            for name, spec in statement.returns.items()
            if not matches_output_spec(outcome.outputs.get(name), spec)
        ]
        extras = sorted(set(outcome.outputs) - set(statement.returns))
        if invalid or extras:
            details: list[str] = []
            if invalid:
                details.append(f"缺失或类型错误 {invalid}")
            if extras:
                details.append(f"未声明输出 {extras}")
            return StatementOutcome.failed("输出合同不满足：" + "；".join(details))
        # A complete-coverage list[record] output must be backed by confirmed evidence, not an
        # unverified terminal guess (design: 「完整覆盖声明必须由可引用事实支持」). This reuses the
        # existing verification grade structurally — no string-vocabulary matching.
        complete_coverage = [
            name for name, spec in statement.returns.items() if spec.coverage == "complete"
        ]
        if complete_coverage and outcome.verification != "confirmed":
            return StatementOutcome.failed(
                "输出合同不满足：完整覆盖声明缺少可信证据 "
                f"{complete_coverage}（verification={outcome.verification}）"
            )
        return outcome

    def _propagate_input_verification(
        self,
        statement: ExecutableStatement,
        outcome: StatementOutcome,
        frames: list[dict[str, JsonValue]],
    ) -> StatementOutcome:
        """Carry data provenance through executor bindings.

        Statement verification describes both the executor's own evidence and the values it
        consumed. Program control dependencies (If conditions and Acquire.source_check) are not
        data provenance: a later confirmed collection may supersede an unverified preflight.
        """
        if not outcome.is_completed or outcome.verification != "confirmed":
            return outcome
        refs = list(statement.inputs.values())
        if isinstance(statement, Command):
            refs.extend(statement.arg_refs.values())
        if any(self._verification(ref, frames) == "accepted_unverified" for ref in refs):
            return outcome.model_copy(update={"verification": "accepted_unverified"})
        return outcome

    def _foreach(
        self,
        loop: ForEach,
        frames: list[dict[str, JsonValue]],
        loop_path: list[int],
    ) -> Generator[StatementInvocation, StatementOutcome, str | None]:
        items, ok = self._resolve(loop.items, frames)
        if not ok:
            return self._fail_control(f"foreach 集合引用不存在：{loop.items.var}")
        if not isinstance(items, list):
            return self._fail_control(
                f"foreach 只接受 list，实际为 {type(items).__name__}"
            )
        collected: list[JsonValue] = []
        collected_verifications = [self._verification(loop.items, frames)]
        for index, item in enumerate(items):
            frame: dict[str, JsonValue] = {loop.item: item}
            if loop.index:
                frame[loop.index] = index
            self._frame_verifications[id(frame)] = {
                loop.item: collected_verifications[0],
                **({loop.index: "confirmed"} if loop.index else {}),
            }
            try:
                reply = yield from self._block(
                    loop.body,
                    frames=[*frames, frame],
                    loop_path=[*loop_path, index],
                )
                if reply is not None:
                    return reply
                if loop.collect is not None:
                    value, found = self._resolve(loop.collect, [*frames, frame])
                    if not found:
                        return self._fail_control(
                            f"foreach collect 引用不存在：{loop.collect.var}"
                        )
                    collected.append(value)
                    collected_verifications.append(
                        self._verification(loop.collect, [*frames, frame])
                    )
            finally:
                self._frame_verifications.pop(id(frame), None)
        if loop.into:
            self._bind(
                loop.into,
                collected,
                frames,
                verification=self._combined_verification(collected_verifications),
            )
        return None

    def _condition(self, cond: Condition, frames: list[dict[str, JsonValue]]) -> bool:
        actual, found = self._resolve(cond.ref, frames)
        if cond.cmp == "exists":
            return found and actual not in (None, "", [], {})
        if cond.cmp == "empty":
            return not found or actual in (None, "", [], {})
        if not found:
            return False
        if cond.cmp == "==":
            return actual == cond.value
        if cond.cmp == "!=":
            return actual != cond.value
        if cond.cmp == "contains":
            return cond.value in actual if isinstance(actual, (str, list, dict)) else False
        if cond.cmp == "not_contains":
            return cond.value not in actual if isinstance(actual, (str, list, dict)) else True
        if cond.cmp == "in":
            return actual in cond.values
        if cond.cmp == "not_in":
            return actual not in cond.values
        try:
            return {
                ">": actual > cond.value,
                ">=": actual >= cond.value,
                "<": actual < cond.value,
                "<=": actual <= cond.value,
            }[cond.cmp]
        except (TypeError, KeyError):
            return False

    def _resolve_map(
        self,
        refs: Mapping[str, ValueRef],
        frames: list[dict[str, JsonValue]],
    ) -> tuple[dict[str, JsonValue], list[str]]:
        values: dict[str, JsonValue] = {}
        missing: list[str] = []
        for name, ref in refs.items():
            value, found = self._resolve(ref, frames)
            if found:
                values[name] = value
            else:
                missing.append(name)
        return values, missing

    def _resolve(
        self,
        ref: ValueRef,
        frames: list[dict[str, JsonValue]],
    ) -> tuple[JsonValue, bool]:
        value: JsonValue
        for frame in reversed(frames):
            if ref.var in frame:
                value = frame[ref.var]
                break
        else:
            if ref.var not in self.env:
                return None, False
            value = self.env[ref.var]
        for part in ref.path:
            try:
                if isinstance(part, int) and isinstance(value, list):
                    value = value[part]
                elif isinstance(part, str) and isinstance(value, dict):
                    value = value[part]
                else:
                    return None, False
            except (IndexError, KeyError):
                return None, False
        return value, True

    def _verification(
        self,
        ref: ValueRef,
        frames: list[dict[str, JsonValue]],
    ) -> Verification:
        for frame in reversed(frames):
            if ref.var in frame:
                return self._frame_verifications.get(id(frame), {}).get(
                    ref.var, "confirmed"
                )
        return self.binding_verifications.get(ref.var, "confirmed")

    @staticmethod
    def _combined_verification(values: Iterable[Verification]) -> Verification:
        return "accepted_unverified" if "accepted_unverified" in values else "confirmed"

    def _bind(
        self,
        name: str,
        value: JsonValue,
        frames: list[dict[str, JsonValue]],
        *,
        verification: Verification = "confirmed",
    ) -> None:
        if frames:
            frames[-1][name] = value
            self._frame_verifications.setdefault(id(frames[-1]), {})[name] = verification
        else:
            self.env[name] = value
            self.binding_verifications[name] = verification

    @staticmethod
    def _render_message(message: str, values: dict[str, JsonValue]) -> str:
        rendered = message
        for name, value in values.items():
            rendered = rendered.replace("{" + name + "}", str(value))
        return rendered

    def _auto_summary(self) -> str:
        if not self.run_log:
            return "任务没有可执行 statement"
        return self.run_log[-1].result.summary

    def _fail_control(self, message: str) -> str:
        self.control_error = message
        return message

    def drive(self, executor: StatementExecutor) -> OrchestratorResult:
        generator = self.steps()
        try:
            current = next(generator)
            while True:
                current = generator.send(executor(current))
        except StopIteration as exc:
            return OrchestratorResult(
                reply=exc.value or "",
                failed=self.failed,
                finish_incomplete=self.finish_incomplete,
                env=dict(self.env),
                run_log=list(self.run_log),
            )

class ProgramRunner:
    """Synchronous convenience wrapper used by unit tests and headless callers."""

    def __init__(self, executor: StatementExecutor) -> None:
        self._executor = executor

    def run(self, program: Program) -> OrchestratorResult:
        return Interpreter(program).drive(self._executor)


__all__ = [
    "InputDescriptor",
    "Interpreter",
    "OrchestratorResult",
    "ProgramRunner",
    "RunRecord",
    "StatementExecutor",
    "StatementInvocation",
    "matches_output_spec",
    "summarize_progress",
]
