"""Production runtime bridge from reviewed Python to Statement executors."""

from __future__ import annotations

import json
import multiprocessing
import traceback
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from gui_agent.core.orchestrator.program import (
    Acquire,
    Command,
    Interact,
    ObservationBinding,
    OutputSpec,
    Read,
)
from gui_agent.core.orchestrator.recovery import RecoveryLedger
from gui_agent.core.orchestrator.runner import RunRecord, StatementInvocation
from gui_agent.core.run.lookup_scope import is_lookup_scope
from gui_agent.core.run.statements.compute_kernel import normalize_table_rows
from gui_agent.core.schemas import StatementContract, StatementOutcome

from .sandbox import SAFE_BUILTINS, validate_code


class CodingProgram(BaseModel):
    """Reviewed restricted-Python planning artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["coding"] = "coding"
    goal: str
    source: str


class CodingCompileError(ValueError):
    def __init__(self, plan: Any) -> None:
        self.plan = plan
        attempt = plan.attempts[-1] if plan.attempts else None
        failures = [
            diagnostic.render()
            for diagnostic in (attempt.diagnostics if attempt is not None else [])
        ]
        if attempt is not None and attempt.run is not None and attempt.run.error:
            failures.append(attempt.run.error)
        if plan.review is not None and plan.review.error:
            failures.append(plan.review.error)
        if (
            plan.review is not None
            and not plan.review.approved
            and not plan.repaired
        ):
            failures.append("coding review rejected the unrepaired program")
        super().__init__(
            "; ".join(failures) or "coding review did not produce an executable program"
        )


def program_from_plan(plan: Any) -> CodingProgram:
    if not plan.requirements_satisfied:
        raise CodingCompileError(plan)
    return CodingProgram(goal=plan.goal, source=plan.source)


@dataclass
class CodingInterpreter:
    """Report-facing execution state shared with the existing result renderer."""

    goal: str
    source: str
    run_log: list[RunRecord] = field(default_factory=list)
    env: dict[str, Any] = field(default_factory=dict)
    binding_contracts: dict[str, dict[str, OutputSpec]] = field(default_factory=dict)
    binding_verifications: dict[str, str] = field(default_factory=dict)
    control_error: str = ""
    finish_incomplete: bool = False

    @property
    def failed(self) -> bool:
        return bool(self.control_error) or any(
            not record.result.is_completed for record in self.run_log
        )

    @property
    def terminal_verification(self) -> str | None:
        completed = [
            record.result.verification
            for record in self.run_log
            if record.result.is_completed and record.result.verification
        ]
        if not completed:
            return "confirmed" if not self.failed else None
        return (
            "accepted_unverified"
            if "accepted_unverified" in completed
            else "confirmed"
        )


class _RuntimeContext:
    """Child-process capability proxy; each request blocks until its Statement ends."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def _request(self, op: str, **payload: Any) -> Any:
        self._connection.send({"kind": "call", "op": op, "payload": payload})
        response = self._connection.recv()
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or f"ctx.{op} failed")
        return response.get("value")

    def query(
        self,
        entity: str,
        fields: list[str],
        filters: dict[str, Any] | None = None,
        field: str = "name",
        fallback: str | None = None,
        coverage: str = "complete",
    ) -> list[dict[str, Any]]:
        scope = self._request(
            "lookup",
            entity=entity,
            field=field,
            fallback=fallback or "",
            filters=filters or {},
            required_fields=fields,
        )
        return self._request(
            "acquire",
            scope=scope,
            fields=fields,
            coverage=coverage,
        )

    def read(
        self,
        target: Any = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        if fields is None:
            raise TypeError("ctx.read requires fields")
        if target is not None:
            self._request("focus", target=target, fields=fields)
        return self._request("read", fields=fields)

    def gui(
        self,
        task: str,
        *,
        target: Any = None,
    ) -> None:
        inputs = {"target": target} if target is not None else {}
        self._request("gui", task=task, inputs=inputs)

    def write(
        self,
        task: str,
        *,
        target: Any = None,
        values: dict[str, Any],
    ) -> None:
        inputs = {"target": target} if target is not None else {}
        self._request("write", task=task, inputs=inputs, values=values)

    def command(self, capability: str, **arguments: Any) -> Any:
        return self._request(
            "command",
            capability=capability,
            arguments=arguments,
        )

def _runtime_worker(source: str, connection: Any) -> None:
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "__name__": "coding_plan",
    }
    try:
        exec(compile(source, "<coding-plan>", "exec"), namespace, namespace)
        result = namespace["run"](_RuntimeContext(connection))
        connection.send({"kind": "return", "value": result})
    except (EOFError, BrokenPipeError):
        pass
    except BaseException:  # noqa: BLE001 - isolate all generated-code failures
        try:
            connection.send({
                "kind": "error",
                "error": traceback.format_exc(limit=8),
            })
        except (EOFError, BrokenPipeError):
            pass
    finally:
        connection.close()


@dataclass
class CodingProgramRuntime:
    """Steppable runtime with the subset of ProgramRuntime used by the agent loop."""

    program: CodingProgram
    interpreter: CodingInterpreter
    current: StatementInvocation | None = None
    index: int = 0
    reply: str | None = None
    current_instance_id: str = ""
    _instance_seq: int = 0
    _statement_seq: int = 0
    _process: Any = None
    _connection: Any = None
    _recovery: RecoveryLedger = field(default_factory=RecoveryLedger)

    @classmethod
    def start(cls, program: CodingProgram) -> "CodingProgramRuntime":
        runtime = cls(
            program=program,
            interpreter=CodingInterpreter(program.goal, program.source),
        )
        diagnostics = validate_code(program.source)
        if diagnostics:
            runtime.interpreter.control_error = "\n".join(
                diagnostic.render() for diagnostic in diagnostics
            )
            runtime.reply = runtime.interpreter.control_error
            return runtime
        parent, child = multiprocessing.get_context("spawn").Pipe()
        runtime._connection = parent
        runtime._process = multiprocessing.get_context("spawn").Process(
            target=_runtime_worker,
            args=(program.source, child),
            daemon=True,
        )
        runtime._process.start()
        child.close()
        runtime._advance()
        return runtime

    @property
    def finished(self) -> bool:
        return self.current is None and self.reply is not None

    def _id(self) -> str:
        self._statement_seq += 1
        return f"c{self._statement_seq}"

    def _invocation(self, op: str, payload: dict[str, Any]) -> StatementInvocation:
        statement_id = self._id()
        if op == "lookup":
            entity = str(payload["entity"])
            field_name = str(payload.get("field") or "name")
            fallback = str(payload.get("fallback") or "")
            filters = dict(payload.get("filters") or {})
            required_fields = [
                str(value) for value in payload.get("required_fields") or []
            ]
            request_text = (
                f"field={field_name!r}, fallback={fallback!r}, "
                f"filters={filters!r}, fields={required_fields!r}"
            )
            statement = Interact(
                id=statement_id,
                goal=(
                    f"Resolve collection {entity!r} in the current business context; "
                    f"{request_text}"
                ),
                success=(
                    f"Exactly one local collection satisfies {request_text}; "
                    "the business context is unchanged"
                ),
                scope="lookup",
                persistence="immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs={
                    "lookup_request": {
                        "entity": entity,
                        "field": field_name,
                        "fallback": fallback,
                        "filters": filters,
                        "required_fields": required_fields,
                    },
                },
            )
        if op == "focus":
            fields = [str(item) for item in payload.get("fields") or []]
            statement = Interact(
                id=statement_id,
                goal="Expose the requested fields for the supplied business target",
                success=f"The target detail state exposes these fields: {fields}",
                observe_fields=fields,
                persistence="immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs={"target": payload.get("target")},
            )
        if op in {"gui", "write"}:
            task = str(payload["task"])
            values = dict(payload.get("values") or {}) if op == "write" else {}
            statement = Interact(
                id=statement_id,
                goal=task,
                success=f"The GUI task is complete: {task}",
                required_values=values,
                persistence="explicit_commit" if values else "immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs=dict(payload.get("inputs") or {}),
            )
        if op == "acquire":
            scope = dict(payload.get("scope") or {})
            if not is_lookup_scope(scope):
                raise ValueError(
                    "internal query scope was not produced by the lookup statement"
                )
            fields = [str(item) for item in payload.get("fields") or []]
            coverage = str(payload.get("coverage") or "complete")
            statement = Acquire(
                id=statement_id,
                goal=f"Materialize records from the established scope {scope.get('entity')!r}",
                required_fields=fields,
                returns={
                    "rows": OutputSpec(
                        type="list[record]",
                        fields=tuple(fields),
                        coverage=coverage,
                    ),
                },
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                args={"lookup_scope": scope},
            )
        if op == "read":
            fields = [str(item) for item in payload.get("fields") or []]
            statement = Read(
                id=statement_id,
                reads={
                    field_name: ObservationBinding(source="field", name=field_name)
                    for field_name in fields
                },
                returns={
                    field_name: OutputSpec(type="json")
                    for field_name in fields
                },
            )
            return StatementInvocation(statement=statement, task_goal=self.program.goal)
        if op == "command":
            statement = Command(
                id=statement_id,
                capability=str(payload["capability"]),
                args=dict(payload.get("arguments") or {}),
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                args=dict(payload.get("arguments") or {}),
            )
        raise ValueError(
            f"ctx.{op} has no production executor; use ordinary Python for computation"
        )

    def _advance(self) -> None:
        if self._connection is None:
            return
        if not self._connection.poll(10.0):
            self._fail("coding program did not yield or finish within 10 seconds")
            return
        message = self._connection.recv()
        kind = message.get("kind")
        if kind == "call":
            try:
                self.current = self._invocation(
                    str(message.get("op") or ""),
                    dict(message.get("payload") or {}),
                )
            except Exception as exc:  # noqa: BLE001 - generated call contract
                self._connection.send({"ok": False, "error": str(exc)})
                self._advance()
            return
        if kind == "return":
            value = message.get("value")
            self.interpreter.env["return"] = value
            self.reply = _render_return(value)
            self.current = None
            self._close_process()
            return
        self._fail(str(message.get("error") or "coding program exited unexpectedly"))

    def _fail(self, error: str) -> None:
        self.interpreter.control_error = error
        self.reply = error
        self.current = None
        self._close_process()

    def _close_process(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process is not None:
            self._process.join(0.2)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(1.0)
            self._process = None

    def close(self) -> None:
        """Release the suspended generated-code process on interruption."""
        self._close_process()

    def next_instance_id(self, statement_id: str = "") -> str:
        if self.current_instance_id:
            raise RuntimeError(
                f"statement instance {self.current_instance_id!r} is still active"
            )
        self._instance_seq += 1
        self.current_instance_id = f"i{self._instance_seq}:{statement_id or 'stmt'}"
        return self.current_instance_id

    def send_outcome(self, outcome: StatementOutcome) -> StatementInvocation | None:
        invocation = self.current
        if invocation is None:
            raise RuntimeError("coding runtime has no active statement")
        instance_id = self.current_instance_id
        self.interpreter.run_log.append(RunRecord(
            node_id=invocation.id,
            executor=invocation.executor,
            name=invocation.goal,
            var=invocation.bind,
            result=outcome,
            loop_path=list(invocation.loop_path),
            instance_id=instance_id,
        ))
        self.current_instance_id = ""
        self.index += 1
        if not outcome.is_completed:
            self._fail(outcome.summary)
            return None
        value: Any = True
        if isinstance(invocation.statement, Interact) and invocation.statement.scope == "lookup":
            value = outcome.outputs.get("scope")
            if not is_lookup_scope(value):
                self._fail("lookup completed without a validated collection scope")
                return None
        elif isinstance(invocation.statement, Acquire):
            rows = outcome.outputs.get("rows", [])
            value = normalize_table_rows(rows if isinstance(rows, list) else [])
        elif isinstance(invocation.statement, Read):
            value = dict(outcome.outputs)
        elif isinstance(invocation.statement, Command):
            value = dict(outcome.outputs) or True
        assert self._connection is not None
        self._connection.send({"ok": True, "value": value})
        self.current = None
        self._advance()
        return self.current

    def retry_current(self, invocation: StatementInvocation) -> None:
        if self.current is None:
            raise RuntimeError("cannot retry without an active statement")
        self.current = invocation

    def restore_current_contract(self, contract: StatementContract) -> None:
        if self.current is None or not isinstance(self.current.statement, Interact):
            raise RuntimeError("only an active Interact has a restorable contract")
        statement = self.current.statement.model_copy(update={
            "goal": contract.goal,
            "success": contract.success,
        })
        self.current = self.current.model_copy(update={"statement": statement})

    def record_recovery(
        self,
        cls: str,
        mechanism: str,
        site: str,
        *,
        detail: str = "",
        outcome: str = "",
    ) -> None:
        self._recovery.record(cls, mechanism, site, detail=detail, outcome=outcome)

    @property
    def has_recovery(self) -> bool:
        return bool(self._recovery.events)

    def recovery_summary(self) -> dict[str, Any]:
        return self._recovery.summary()


def _render_return(value: Any) -> str:
    if value is None:
        return "Coding program completed"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


__all__ = [
    "CodingCompileError",
    "CodingProgram",
    "CodingProgramRuntime",
    "program_from_plan",
]
