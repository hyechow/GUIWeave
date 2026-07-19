"""Steppable interpreter for the semantic Program IR.

Only ``Interact``, ``Data`` and ``Command`` cross an executor boundary.  The
interpreter owns all explicit branching, deterministic iteration and typed
value binding.  It never collects UI rows, writes SQL or asks an LLM to expand
the Program at runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, JsonValue

from gui_agent.core.schemas import StatementOutcome

from .program import (
    Command,
    Condition,
    Data,
    ExecutableStatement,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    StatementNode,
    Stmt,
    ValueRef,
)


ExecutorKind = Literal["interact", "data", "command", "program"]
StatementExecutor = Callable[["StatementInvocation"], StatementOutcome]


class StatementInvocation(BaseModel):
    """One resolved executor call yielded by the Program interpreter."""

    statement: ExecutableStatement = Field(discriminator="op")
    task_goal: str = ""
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
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
    def executor(self) -> Literal["interact", "data", "command"]:
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
    experience = "\n".join(
        f"{'✓' if record.result.is_completed else '✗'} [{record.executor}] "
        f"{record.name}（{record.result.summary}）"
        + (f" outputs={record.result.outputs}" if record.result.outputs else "")
        for record in run_log
    )
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

    @property
    def accepted_unverified(self) -> bool:
        return any(
            record.result.verification == "accepted_unverified"
            for record in self.run_log
        )


def _value_matches(value: JsonValue, spec: OutputSpec) -> bool:
    if value is None:
        return not spec.required
    if spec.type in {"text", "url"}:
        return isinstance(value, str) and (bool(value.strip()) or not spec.required)
    if spec.type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if spec.type == "boolean":
        return isinstance(value, bool)
    if spec.type == "record":
        return isinstance(value, dict)
    if spec.type == "list[record]":
        return isinstance(value, list) and all(isinstance(row, dict) for row in value)
    return True


class Interpreter:
    """Program state and deterministic control-flow owner."""

    def __init__(self, program: Program) -> None:
        self._program = program
        self.env: dict[str, JsonValue] = {}
        self.run_log: list[RunRecord] = []
        self.finish_incomplete = False
        self.finish_outputs: dict[str, JsonValue] = {}
        self.control_error = ""

    @property
    def failed(self) -> bool:
        return bool(self.control_error) or any(
            not record.result.is_completed for record in self.run_log
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
            if isinstance(statement, (Interact, Data, Command)):
                invocation, error = self._invocation(statement, frames, loop_path)
                if error:
                    outcome = StatementOutcome.failed(error)
                else:
                    outcome = yield invocation
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
                    self._bind(statement.bind, dict(outcome.outputs), frames)
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
                self.finish_outputs = values
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
            args=args,
            loop_path=list(loop_path),
        ), ""

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
            if not _value_matches(outcome.outputs.get(name), spec)
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
        for index, item in enumerate(items):
            frame: dict[str, JsonValue] = {loop.item: item}
            if loop.index:
                frame[loop.index] = index
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
        if loop.into:
            self._bind(loop.into, collected, frames)
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

    def _bind(self, name: str, value: JsonValue, frames: list[dict[str, JsonValue]]) -> None:
        if frames:
            frames[-1][name] = value
        else:
            self.env[name] = value

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
    "Interpreter",
    "OrchestratorResult",
    "ProgramRunner",
    "RunRecord",
    "StatementExecutor",
    "StatementInvocation",
    "summarize_progress",
]
