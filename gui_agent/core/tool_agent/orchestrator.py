"""Reviewed-Python Master for orchestrating autonomous tool-agent Workers.

The program produced here describes task-level control and data flow.  It is
deliberately unable to issue GUI actions: those remain inside ``gui_worker`` and
its screenshot-driven loop.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from jsonschema import Draft202012Validator
from langchain_core.messages import HumanMessage, SystemMessage

from gui_agent.core.tool_agent.contracts import WorkerOutcome, WorkerSpec
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import (
    diagnostic_prompt_reports,
    message_text,
    response_usage,
)
from gui_agent.core.tool_agent.sandbox import (
    execute_transform,
    validate_transform_row_fields,
    validate_transform_source,
)


_MASTER_FILENAME = "<tool-agent-master>"
_WORKER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CTX_METHODS = {
    "gui_worker",
    "data_worker",
    "worker_result",
    "finish",
    "replan",
    "fail",
}
_SAFE_METHODS = {
    "append",
    "copy",
    "count",
    "endswith",
    "extend",
    "get",
    "index",
    "items",
    "join",
    "keys",
    "lower",
    "pop",
    "setdefault",
    "sort",
    "split",
    "startswith",
    "strip",
    "upper",
    "values",
}
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
_BANNED_NAMES = {
    "__builtins__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "open",
    "setattr",
    "vars",
}
_BANNED_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Lambda,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.While,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


@dataclass(frozen=True)
class MasterDiagnostic:
    code: str
    message: str
    line: int = 0

    def render(self) -> str:
        location = f"line {self.line}" if self.line else "source"
        return f"[{self.code}] {location}: {self.message}"


@dataclass(frozen=True)
class MasterProgram:
    source: str
    attempts: int


@dataclass(frozen=True)
class MasterTerminal:
    phase: Literal["completed", "failed", "replan"]
    summary: str
    result_ref: str = ""


@dataclass(frozen=True)
class MasterExecution:
    terminal: MasterTerminal | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.terminal is not None and not self.error


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    kind: Literal["gui", "data"]
    goal: str
    signature: str
    outcome: WorkerOutcome

    def summary(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "kind": self.kind,
            "goal": self.goal,
            "outcome": self.outcome.model_dump(mode="json"),
        }


class MasterCompileError(ValueError):
    pass


class _ProgramHalt(BaseException):
    pass


def _diagnostic(code: str, message: str, node: ast.AST | None = None) -> MasterDiagnostic:
    return MasterDiagnostic(code=code, message=message, line=getattr(node, "lineno", 0))


def _literal_keyword(call: ast.Call, name: str) -> Any:
    keyword = next((item for item in call.keywords if item.arg == name), None)
    if keyword is None:
        raise ValueError(f"missing required keyword {name!r}")
    try:
        return ast.literal_eval(keyword.value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise ValueError(f"keyword {name!r} must be a literal value") from exc


def _validate_worker_id(call: ast.Call) -> list[MasterDiagnostic]:
    try:
        worker_id = _literal_keyword(call, "worker_id")
    except ValueError as exc:
        return [_diagnostic("WORKER_ID", str(exc), call)]
    if not isinstance(worker_id, str) or _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
        return [_diagnostic("WORKER_ID", "worker_id must be a stable snake_case identifier", call)]
    return []


def _validate_gui_worker_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics = _validate_worker_id(call)
    required = {
        "goal",
        "success_criteria",
        "data_requirements",
        "actions",
        "result_schema",
    }
    values: dict[str, Any] = {}
    for name in required:
        try:
            values[name] = _literal_keyword(call, name)
        except ValueError as exc:
            diagnostics.append(_diagnostic("GUI_WORKER_LITERAL", str(exc), call))
    if diagnostics:
        return diagnostics
    max_steps = 8
    if any(item.arg == "max_steps" for item in call.keywords):
        try:
            max_steps = _literal_keyword(call, "max_steps")
        except ValueError as exc:
            return [_diagnostic("GUI_WORKER_LITERAL", str(exc), call)]
    try:
        profile = None
        if any(item.arg == "profile" for item in call.keywords):
            profile = _literal_keyword(call, "profile")
        spec = WorkerSpec.model_validate(
            {**values, "profile": profile, "max_steps": max_steps}
        )
        Draft202012Validator.check_schema(spec.result_schema)
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        combined_row_schema = {
            "type": "object",
            "properties": {
                key: value
                for requirement in spec.data_requirements
                for key, value in (requirement.row_schema.get("properties") or {}).items()
            },
        }
        for action in spec.actions:
            if action.capability == "python_transform":
                source = str(action.fixed_args.get("source") or "")
                validate_transform_source(source)
                validate_transform_row_fields(source, combined_row_schema)
    except Exception as exc:  # noqa: BLE001 - surfaced as a compile diagnostic
        diagnostics.append(_diagnostic("GUI_WORKER_SPEC", str(exc), call))
    return diagnostics


def _validate_data_worker_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics = _validate_worker_id(call)
    for name in ("goal", "source", "result_schema"):
        try:
            value = _literal_keyword(call, name)
            if name == "goal" and (not isinstance(value, str) or not value.strip()):
                raise ValueError("goal must be a non-empty string")
            if name == "source":
                if not isinstance(value, str):
                    raise ValueError("source must be a string")
                validate_transform_source(value)
            if name == "result_schema":
                Draft202012Validator.check_schema(value)
        except Exception as exc:  # noqa: BLE001 - one deterministic diagnostic channel
            diagnostics.append(_diagnostic("DATA_WORKER_SPEC", f"{name}: {exc}", call))
    if not any(item.arg == "inputs" for item in call.keywords):
        diagnostics.append(_diagnostic("DATA_WORKER_SPEC", "missing required keyword 'inputs'", call))
    else:
        inputs = next(item.value for item in call.keywords if item.arg == "inputs")
        if isinstance(inputs, (ast.List, ast.Tuple)):
            for item in inputs.elts:
                if _subscript_key(item) == "result_ref":
                    diagnostics.append(_diagnostic(
                        "REF_VALUE_REQUIRED",
                        "data_worker inputs require result_ref['ref'], not the result_ref descriptor",
                        item,
                    ))
    return diagnostics


def _subscript_key(node: ast.AST) -> str:
    if not isinstance(node, ast.Subscript):
        return ""
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return ""


def _validate_finish_call(call: ast.Call) -> list[MasterDiagnostic]:
    value: ast.AST | None = call.args[0] if call.args else None
    keyword = next((item for item in call.keywords if item.arg == "result_ref"), None)
    if keyword is not None:
        value = keyword.value
    if value is None:
        return [_diagnostic("FINISH_SIGNATURE", "ctx.finish requires a result ref string", call)]
    if _subscript_key(value) == "result_ref":
        return [_diagnostic(
            "REF_VALUE_REQUIRED",
            "ctx.finish requires result_ref['ref'], not the result_ref descriptor",
            value,
        )]
    return []


def validate_master_source(source: str) -> list[MasterDiagnostic]:
    """Validate one restricted Worker-orchestration program."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return [MasterDiagnostic("SYNTAX", str(exc), exc.lineno or 0)]

    diagnostics: list[MasterDiagnostic] = []
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(tree.body) != 1 or len(functions) != 1 or functions[0].name != "run":
        diagnostics.append(_diagnostic("PROGRAM_SHAPE", "source must contain exactly def run(ctx):"))
    else:
        fn = functions[0]
        if (
            len(fn.args.args) != 1
            or fn.args.args[0].arg != "ctx"
            or fn.args.vararg is not None
            or fn.args.kwarg is not None
            or fn.args.kwonlyargs
            or fn.decorator_list
        ):
            diagnostics.append(_diagnostic("RUN_SIGNATURE", "run must accept exactly one argument named ctx", fn))

    terminal_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            diagnostics.append(_diagnostic("UNSAFE_SYNTAX", f"{type(node).__name__} is disallowed", node))
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            diagnostics.append(_diagnostic("UNSAFE_NAME", f"{node.id!r} is disallowed", node))
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                diagnostics.append(_diagnostic("PRIVATE_ATTRIBUTE", "private/dunder access is disallowed", node))
            elif not (
                isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
                or node.attr in _SAFE_METHODS
            ):
                diagnostics.append(_diagnostic(
                    "ATTRIBUTE_ACCESS",
                    "Master values are JSON-like dicts; use ['ref'] rather than object attributes",
                    node,
                ))
        if not isinstance(node, ast.Call):
            continue
        if any(item.arg is None for item in node.keywords):
            diagnostics.append(_diagnostic("KEYWORD_SPLAT", "**kwargs calls are disallowed", node))
        if isinstance(node.func, ast.Name):
            if node.func.id not in _SAFE_BUILTINS:
                diagnostics.append(_diagnostic("UNSAFE_CALL", f"call to {node.func.id!r} is disallowed", node))
            continue
        if not isinstance(node.func, ast.Attribute):
            diagnostics.append(_diagnostic("UNSAFE_CALL", "indirect calls are disallowed", node))
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx":
            method = node.func.attr
            if method not in _CTX_METHODS:
                diagnostics.append(_diagnostic("UNKNOWN_CTX_API", f"ctx.{method} is not available", node))
                continue
            if method in {"gui_worker", "data_worker"} and node.args:
                diagnostics.append(_diagnostic("WORKER_SIGNATURE", f"ctx.{method} accepts keyword arguments only", node))
            if method == "gui_worker":
                diagnostics.extend(_validate_gui_worker_call(node))
            elif method == "data_worker":
                diagnostics.extend(_validate_data_worker_call(node))
            elif method in {"finish", "replan", "fail"}:
                terminal_calls += 1
                if method == "finish":
                    diagnostics.extend(_validate_finish_call(node))
            continue
        if node.func.attr not in _SAFE_METHODS:
            diagnostics.append(_diagnostic("UNSAFE_METHOD", f"method {node.func.attr!r} is disallowed", node))

    if terminal_calls == 0:
        diagnostics.append(_diagnostic("TERMINAL_REQUIRED", "program must call ctx.finish, ctx.replan, or ctx.fail"))
    unique: list[MasterDiagnostic] = []
    seen: set[tuple[str, str, int]] = set()
    for item in diagnostics:
        key = (item.code, item.message, item.line)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _extract_source(content: Any) -> str:
    text = message_text(content).strip()
    fenced = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


def compile_master_program(
    *,
    llm: Any,
    system_prompt: str,
    task_context: dict[str, Any],
    execution_history: list[dict[str, Any]],
    feedback: str = "",
    max_attempts: int = 3,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> MasterProgram:
    """Generate and deterministically review a complete orchestration program."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    generator = (
        llm.bind(max_tokens=12_000, extra_body={"enable_thinking": False})
        if callable(getattr(llm, "bind", None))
        else llm
    )
    rejected = ""
    last_diagnostics: list[MasterDiagnostic] = []
    for attempt in range(1, max_attempts + 1):
        payload = {
            "task": task_context,
            "completed_worker_history": execution_history,
            "runtime_feedback": feedback or "(none)",
        }
        if rejected:
            payload["rejected_program"] = rejected
            payload["validation_issues"] = [item.render() for item in last_diagnostics]
        started_at = time.perf_counter()
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        response = generator.invoke(messages)
        llm_elapsed_s = time.perf_counter() - started_at
        source = _extract_source(response.content)
        diagnostics = validate_master_source(source)
        if on_event is not None:
            on_event(
                "master_compile_attempt",
                {
                    "attempt": attempt,
                    "source": source,
                    "diagnostics": [item.render() for item in diagnostics],
                    "llm_elapsed_s": round(llm_elapsed_s, 3),
                    "token_usage": response_usage(response),
                    "context_reports": diagnostic_prompt_reports(
                        "tool_agent.master",
                        messages,
                        response,
                        parsed={
                            "source": source,
                            "diagnostics": [item.render() for item in diagnostics],
                        },
                        schema="Reviewed Python program",
                    ),
                },
            )
        if not diagnostics:
            return MasterProgram(source=source, attempts=attempt)
        rejected = source
        last_diagnostics = diagnostics
    rendered = "; ".join(item.render() for item in last_diagnostics)
    raise MasterCompileError(f"Master program failed review after {max_attempts} attempts: {rendered}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkerOrchestrationContext:
    """Runtime API exposed to reviewed Master programs."""

    def __init__(
        self,
        *,
        data_store: RuntimeDataStore,
        run_gui_worker: Callable[[str, WorkerSpec], WorkerOutcome],
        trace: Callable[..., None],
    ) -> None:
        self._data_store = data_store
        self._run_gui_worker = run_gui_worker
        self._trace = trace
        self._records: dict[str, WorkerRecord] = {}
        self._terminal: MasterTerminal | None = None

    @property
    def terminal(self) -> MasterTerminal | None:
        return self._terminal

    def history_for_model(self) -> list[dict[str, Any]]:
        return [record.summary() for record in self._records.values()]

    def _reuse(self, worker_id: str, signature: str) -> dict[str, Any] | None:
        record = self._records.get(worker_id)
        if record is None:
            return None
        if record.signature != signature:
            raise ValueError(
                f"worker_id {worker_id!r} is already bound to a different specification; "
                "use a new worker_id for a retry or changed subgoal"
            )
        self._trace(
            "master_worker_reuse",
            worker_id=worker_id,
            kind=record.kind,
            outcome=record.outcome.model_dump(mode="json"),
        )
        return record.outcome.model_dump(mode="json")

    def gui_worker(
        self,
        *,
        worker_id: str,
        profile: Literal["operator", "collector"] | None = None,
        goal: str,
        success_criteria: list[str],
        data_requirements: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        result_schema: dict[str, Any],
        max_steps: int = 8,
    ) -> dict[str, Any]:
        if _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
            raise ValueError("worker_id must be a stable snake_case identifier")
        spec = WorkerSpec.model_validate(
            {
                "profile": profile,
                "goal": goal,
                "success_criteria": success_criteria,
                "data_requirements": data_requirements,
                "actions": actions,
                "result_schema": result_schema,
                "max_steps": max_steps,
            }
        )
        signature = hashlib.sha256(_canonical(spec.model_dump(mode="json")).encode()).hexdigest()
        reused = self._reuse(worker_id, signature)
        if reused is not None:
            return reused
        self._trace(
            "master_worker_dispatch",
            worker_id=worker_id,
            kind="gui",
            goal=goal,
            spec=spec.model_dump(mode="json"),
        )
        try:
            outcome = self._run_gui_worker(worker_id, spec)
        except Exception as exc:  # noqa: BLE001 - a Worker failure is typed program data
            outcome = WorkerOutcome(
                phase="failed",
                summary=f"GUI Worker runtime error: {type(exc).__name__}: {exc}",
                steps=0,
            )
        self._records[worker_id] = WorkerRecord(worker_id, "gui", goal, signature, outcome)
        self._trace(
            "master_worker_result",
            worker_id=worker_id,
            kind="gui",
            outcome=outcome.model_dump(mode="json"),
        )
        return outcome.model_dump(mode="json")

    def data_worker(
        self,
        *,
        worker_id: str,
        goal: str,
        inputs: list[str],
        source: str,
        result_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
            raise ValueError("worker_id must be a stable snake_case identifier")
        if not isinstance(inputs, list) or not inputs or any(not isinstance(item, str) for item in inputs):
            raise ValueError("data_worker inputs must be a non-empty list of data refs")
        Draft202012Validator.check_schema(result_schema)
        validate_transform_source(source)
        request = {
            "goal": goal,
            "inputs": inputs,
            "source": source,
            "result_schema": result_schema,
        }
        signature = hashlib.sha256(_canonical(request).encode()).hexdigest()
        reused = self._reuse(worker_id, signature)
        if reused is not None:
            return reused
        self._trace("data_worker_start", worker_id=worker_id, goal=goal, inputs=inputs, source=source)
        try:
            input_values = [self._data_store.resolve_value(item) for item in inputs]
            value = execute_transform(source, input_values, result_schema)
            descriptor = self._data_store.put_result(
                value,
                result_schema,
                summary=f"Data Worker {worker_id} completed: {goal}",
            )
            outcome = WorkerOutcome(
                phase="completed",
                summary=descriptor.summary,
                result_ref=descriptor,
                steps=1,
            )
        except Exception as exc:  # noqa: BLE001 - branchable typed outcome
            outcome = WorkerOutcome(
                phase="failed",
                summary=f"Data Worker error: {type(exc).__name__}: {exc}",
                steps=1,
            )
        self._records[worker_id] = WorkerRecord(worker_id, "data", goal, signature, outcome)
        self._trace(
            "data_worker_complete",
            worker_id=worker_id,
            outcome=outcome.model_dump(mode="json"),
        )
        return outcome.model_dump(mode="json")

    def worker_result(self, worker_id: str) -> dict[str, Any] | None:
        record = self._records.get(worker_id)
        return record.outcome.model_dump(mode="json") if record is not None else None

    def finish(self, result_ref: str) -> None:
        descriptor = self._data_store.result_descriptor(result_ref)
        self._terminal = MasterTerminal(
            phase="completed",
            summary=descriptor.summary or "Coding Master accepted the ResultRef.",
            result_ref=descriptor.ref,
        )
        raise _ProgramHalt

    def replan(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("replan requires a concrete reason")
        self._terminal = MasterTerminal(phase="replan", summary=reason.strip())
        raise _ProgramHalt

    def fail(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("fail requires a concrete reason")
        self._terminal = MasterTerminal(phase="failed", summary=reason.strip())
        raise _ProgramHalt

    def reset_terminal(self) -> None:
        self._terminal = None


def execute_master_program(
    source: str,
    ctx: WorkerOrchestrationContext,
    *,
    max_lines: int = 10_000,
) -> MasterExecution:
    """Execute one reviewed orchestration program with a bounded line budget."""
    diagnostics = validate_master_source(source)
    if diagnostics:
        return MasterExecution(error="; ".join(item.render() for item in diagnostics))
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    ctx.reset_terminal()
    try:
        exec(compile(source, _MASTER_FILENAME, "exec"), namespace, namespace)
        lines = 0

        def tracer(frame: Any, event: str, arg: Any) -> Any:
            del arg
            nonlocal lines
            if event == "line" and frame.f_code.co_filename == _MASTER_FILENAME:
                lines += 1
                if lines > max_lines:
                    raise RuntimeError(f"Master program exceeded {max_lines} executed lines")
            return tracer

        previous_trace = sys.gettrace()
        sys.settrace(tracer)
        try:
            namespace["run"](ctx)
        except _ProgramHalt:
            pass
        finally:
            sys.settrace(previous_trace)
        if ctx.terminal is None:
            return MasterExecution(error="Master program returned without ctx.finish, ctx.replan, or ctx.fail")
        return MasterExecution(terminal=ctx.terminal)
    except BaseException as exc:  # noqa: BLE001 - sandbox boundary reports errors as data
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return MasterExecution(error=f"{type(exc).__name__}: {exc}")


__all__ = [
    "MasterCompileError",
    "MasterDiagnostic",
    "MasterExecution",
    "MasterProgram",
    "MasterTerminal",
    "WorkerOrchestrationContext",
    "compile_master_program",
    "execute_master_program",
    "validate_master_source",
]
