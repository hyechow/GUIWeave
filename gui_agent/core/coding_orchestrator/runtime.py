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
from gui_agent.core.filter_contract import compile_filter_predicates
from gui_agent.core.schemas import (
    CollectionIntent,
    StatementContract,
    StatementOutcome,
)

from .sandbox import SAFE_BUILTINS, validate_code
from .models import UIStateHandle, collection_postcondition, require_ui_state


class CodingProgram(BaseModel):
    """Reviewed restricted-Python planning artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["coding"] = "coding"
    goal: str
    source: str


def _report_coding_payload(op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Project a ctx.* call payload into a small, JSON-safe report form."""

    def _clip(value: Any, *, depth: int = 0) -> Any:
        if depth > 3:
            return "…"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if len(value) <= 240 else value[:237] + "…"
        if isinstance(value, list):
            return [_clip(item, depth=depth + 1) for item in value[:20]]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 24:
                    out["…"] = f"+{len(value) - 24} keys"
                    break
                out[str(key)] = _clip(item, depth=depth + 1)
            return out
        text = repr(value)
        return text if len(text) <= 120 else text[:117] + "…"

    if not payload:
        return {}

    def _state_token(value: Any) -> str:
        return value.token if isinstance(value, UIStateHandle) else ""

    # Prefer the structured request keys that the report data panel already understands.
    if op == "gui":
        return {
            "goal": payload.get("goal"),
            "success": payload.get("success"),
            **({"target": payload.get("target")} if payload.get("target") is not None else {}),
        }
    if op == "write":
        return {
            "task": payload.get("task"),
            **(dict(payload.get("inputs") or {}) if isinstance(payload.get("inputs"), dict) else {}),
            "values": payload.get("values") or {},
        }
    if op == "lookup":
        return {
            "state": _state_token(payload.get("state")),
            "entity": payload.get("entity"),
            "field": payload.get("field"),
            "fallback": payload.get("fallback"),
            "filters": payload.get("filters") or {},
            "required_fields": payload.get("required_fields") or [],
        }
    if op == "constrain":
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        return {
            "state": scope.get("ui_state_token"),
            "entity": payload.get("entity"),
            "filters": payload.get("filters") or {},
        }
    if op == "acquire":
        scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        return {
            "state": scope.get("ui_state_token"),
            "entity": scope.get("entity"),
            "fields": payload.get("fields") or [],
            "coverage": payload.get("coverage") or "complete",
        }
    if op == "focus":
        return {
            "state": _state_token(payload.get("state")),
            "target": payload.get("target"),
            "fields": payload.get("fields") or [],
        }
    if op == "read":
        return {
            "state": _state_token(payload.get("state")),
            "fields": payload.get("fields") or [],
        }
    if op == "command":
        return {
            "capability": payload.get("capability"),
            **(dict(payload.get("arguments") or {}) if isinstance(payload.get("arguments"), dict) else {}),
        }
    return {key: _clip(value) for key, value in payload.items()}


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

    def _request(
        self,
        op: str,
        *,
        plan: str = "",
        plan_step: int = 0,
        plan_steps: int = 0,
        **payload: Any,
    ) -> Any:
        """Send one internal op. ``plan*`` tags the plan-level API expansion (e.g. query)."""
        self._connection.send({
            "kind": "call",
            "op": op,
            "payload": payload,
            "plan": plan or op,
            "plan_step": int(plan_step or 1),
            "plan_steps": int(plan_steps or 1),
        })
        response = self._connection.recv()
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or f"ctx.{op} failed")
        return response.get("value")

    def query(
        self,
        state: UIStateHandle,
        *,
        entity: str,
        fields: list[str],
        filters: dict[str, Any] | None = None,
        field: str = "name",
        fallback: str | None = None,
        coverage: str = "complete",
    ) -> list[dict[str, Any]]:
        # Macro: locate source, establish the exact filter state, then materialize rows.
        scope = self._request(
            "lookup",
            plan="query",
            plan_step=1,
            plan_steps=3,
            state=state,
            entity=entity,
            field=field,
            fallback=fallback or "",
            required_fields=fields,
        )
        scope = self._request(
            "constrain",
            plan="query",
            plan_step=2,
            plan_steps=3,
            state=state,
            scope=scope,
            entity=entity,
            filters=dict(filters or {}),
        )
        return self._request(
            "acquire",
            plan="query",
            plan_step=3,
            plan_steps=3,
            state=state,
            scope=scope,
            fields=fields,
            coverage=coverage,
        )

    def read(
        self,
        state: UIStateHandle,
        *,
        target: Any = None,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        if fields is None:
            raise TypeError("ctx.read requires fields")
        if target is not None:
            state = self._request(
                "focus",
                plan="read",
                plan_step=1,
                plan_steps=2,
                state=state,
                target=target,
                fields=fields,
            )
            return self._request(
                "read",
                plan="read",
                plan_step=2,
                plan_steps=2,
                state=state,
                fields=fields,
            )
        return self._request(
            "read",
            plan="read",
            plan_step=1,
            plan_steps=1,
            state=state,
            fields=fields,
        )

    def gui(
        self,
        goal: str,
        *,
        success: dict[str, Any],
        target: Any = None,
    ) -> UIStateHandle:
        return self._request(
            "gui",
            plan="gui",
            plan_step=1,
            plan_steps=1,
            goal=goal,
            success=success,
            target=target,
        )

    def write(
        self,
        task: str,
        *,
        target: Any = None,
        values: dict[str, Any],
    ) -> None:
        inputs = {"target": target} if target is not None else {}
        self._request(
            "write",
            plan="write",
            plan_step=1,
            plan_steps=1,
            task=task,
            inputs=inputs,
            values=values,
        )

    def command(self, capability: str, **arguments: Any) -> Any:
        return self._request(
            "command",
            plan="command",
            plan_step=1,
            plan_steps=1,
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
    current_coding_op: str = ""
    current_coding_payload: dict[str, Any] = field(default_factory=dict)
    current_coding_plan: str = ""
    current_coding_plan_step: int = 0
    current_coding_plan_steps: int = 0
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

    @staticmethod
    def _state_context(state: UIStateHandle) -> dict[str, Any]:
        return {
            "inputs": {"ui_state": state.snapshot()},
            "args": {"ui_state_token": state.token},
        }

    def _invocation(self, op: str, payload: dict[str, Any]) -> StatementInvocation:
        statement_id = self._id()
        if op == "lookup":
            entity = str(payload["entity"])
            field_name = str(payload.get("field") or "name")
            fallback = str(payload.get("fallback") or "")
            required_fields = [
                str(value) for value in payload.get("required_fields") or []
            ]
            state = require_ui_state(
                payload.get("state"),
                entity=entity,
                fields=required_fields,
            )
            request_text = (
                f"field={field_name!r}, fallback={fallback!r}, fields={required_fields!r}"
            )
            statement = Interact(
                id=statement_id,
                goal=(
                    f"Locate collection {entity!r} as the single local source in the current "
                    f"business context; {request_text}"
                ),
                success=(
                    f"Exactly one local collection satisfies {request_text}; "
                    "the business context is unchanged"
                ),
                interaction_intent=CollectionIntent(
                    phase="locate",
                    entity=entity,
                    field=field_name,
                    fallback=fallback,
                    required_fields=required_fields,
                ),
                persistence="immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                **self._state_context(state),
            )
        if op == "constrain":
            scope = dict(payload.get("scope") or {})
            state = require_ui_state(payload.get("state"))
            entity = str(payload["entity"])
            if str(scope.get("entity") or "") != entity:
                raise ValueError(
                    "constrain entity does not match its collection scope"
                )
            filters = dict(payload.get("filters") or {})
            statement = Interact(
                id=statement_id,
                goal=f"Narrow collection {entity!r} to the source-native filter {filters!r}",
                success=(
                    f"The filter {filters!r} is active on the current {entity!r} view"
                ),
                interaction_intent=CollectionIntent(
                    phase="constrain",
                    entity=entity,
                    predicates=compile_filter_predicates(filters),
                ),
                persistence="immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs={"ui_state": state.snapshot()},
                args={"lookup_scope": scope, "ui_state_token": state.token},
            )
        if op == "focus":
            state = require_ui_state(payload.get("state"))
            fields = [str(item) for item in payload.get("fields") or []]
            target = payload.get("target")
            statement = Interact(
                id=statement_id,
                goal=f"Expose fields {fields} for business target {target!r}",
                success=f"The target detail state exposes these fields: {fields}",
                observe_fields=fields,
                persistence="immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs={
                    "ui_state": state.snapshot(),
                    "target": target,
                },
                args={"ui_state_token": state.token},
            )
        if op in {"gui", "write"}:
            task = str(payload.get("goal") if op == "gui" else payload["task"])
            values = dict(payload.get("values") or {}) if op == "write" else {}
            interaction_intent = None
            success_text = f"The GUI task is complete: {task}"
            if op == "gui":
                success = collection_postcondition(payload.get("success"))
                if success is None:
                    raise ValueError("ctx.gui success is not a collection postcondition")
                entity, required_fields = success["entity"], success["fields"]
                interaction_intent = CollectionIntent(
                    phase="reach",
                    entity=entity,
                    required_fields=required_fields,
                )
                success_text = (
                    f"One structural collection for {entity!r} is available "
                    f"with fields {required_fields!r}"
                )
            statement = Interact(
                id=statement_id,
                goal=task,
                success=success_text,
                **(
                    {"interaction_intent": interaction_intent}
                    if interaction_intent is not None
                    else {}
                ),
                required_values=values,
                persistence="explicit_commit" if values else "immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs=(
                    {"target": payload.get("target")}
                    if op == "gui" and payload.get("target") is not None
                    else dict(payload.get("inputs") or {})
                ),
            )
        if op == "acquire":
            scope = dict(payload.get("scope") or {})
            if not is_lookup_scope(scope):
                raise ValueError(
                    "internal query scope was not produced by the lookup statement"
                )
            state = require_ui_state(payload.get("state"))
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
                inputs={"ui_state": state.snapshot()},
                args={"lookup_scope": scope, "ui_state_token": state.token},
            )
        if op == "read":
            state = require_ui_state(payload.get("state"))
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
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                **self._state_context(state),
            )
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
            op = str(message.get("op") or "")
            payload = dict(message.get("payload") or {})
            plan = str(message.get("plan") or op or "")
            plan_step = int(message.get("plan_step") or 1)
            plan_steps = int(message.get("plan_steps") or 1)
            try:
                self.current = self._invocation(op, payload)
                self.current_coding_op = op
                self.current_coding_payload = _report_coding_payload(op, payload)
                self.current_coding_plan = plan
                self.current_coding_plan_step = plan_step
                self.current_coding_plan_steps = plan_steps
            except Exception as exc:  # noqa: BLE001 - generated call contract
                self.current_coding_op = ""
                self.current_coding_payload = {}
                self.current_coding_plan = ""
                self.current_coding_plan_step = 0
                self.current_coding_plan_steps = 0
                self._connection.send({"ok": False, "error": str(exc)})
                self._advance()
            return
        if kind == "return":
            value = message.get("value")
            self.interpreter.env["return"] = value
            self.reply = _render_return(value)
            self.current = None
            self.current_coding_op = ""
            self.current_coding_payload = {}
            self.current_coding_plan = ""
            self.current_coding_plan_step = 0
            self.current_coding_plan_steps = 0
            self._close_process()
            return
        self._fail(str(message.get("error") or "coding program exited unexpectedly"))

    def _fail(self, error: str) -> None:
        self.interpreter.control_error = error
        self.reply = error
        self.current = None
        self.current_coding_op = ""
        self.current_coding_payload = {}
        self.current_coding_plan = ""
        self.current_coding_plan_step = 0
        self.current_coding_plan_steps = 0
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
        coding_op = self.current_coding_op
        coding_payload = dict(self.current_coding_payload)
        issued_ui_state: UIStateHandle | None = None
        if outcome.is_completed and coding_op in {"gui", "focus"}:
            if coding_op == "focus":
                postcondition = {
                    "kind": "target_fields_available",
                    "target": invocation.inputs.get("target"),
                    "fields": list(invocation.statement.observe_fields),
                }
            else:
                postcondition = dict(coding_payload.get("success") or {})
            issued_ui_state = UIStateHandle(
                token=f"{invocation.id}:state",
                postcondition=postcondition,
                observed_state=dict(outcome.outputs),
            )
            coding_payload["produced_state"] = issued_ui_state.token
        self.interpreter.run_log.append(RunRecord(
            node_id=invocation.id,
            executor=invocation.executor,
            name=invocation.goal,
            var=invocation.bind,
            result=outcome,
            loop_path=list(invocation.loop_path),
            instance_id=instance_id,
            coding_op=coding_op,
            coding_payload=coding_payload,
            coding_plan=self.current_coding_plan,
            coding_plan_step=self.current_coding_plan_step,
            coding_plan_steps=self.current_coding_plan_steps,
        ))
        self.current_instance_id = ""
        self.current_coding_op = ""
        self.current_coding_payload = {}
        self.current_coding_plan = ""
        self.current_coding_plan_step = 0
        self.current_coding_plan_steps = 0
        self.index += 1
        if not outcome.is_completed:
            self._fail(outcome.summary)
            return None
        value: Any = True
        if (
            isinstance(invocation.statement, Interact)
            and invocation.statement.interaction_intent is not None
            and invocation.statement.interaction_intent.phase == "locate"
        ):
            value = outcome.outputs.get("scope")
            if not is_lookup_scope(value):
                self._fail("lookup completed without a validated collection scope")
                return None
        elif (
            isinstance(invocation.statement, Interact)
            and invocation.statement.interaction_intent is not None
            and invocation.statement.interaction_intent.phase == "constrain"
        ):
            scope = dict(invocation.args.get("lookup_scope") or {})
            if not is_lookup_scope(scope):
                self._fail("constrain completed without its collection scope")
                return None
            value = scope
        elif issued_ui_state is not None:
            value = issued_ui_state
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
            "interaction_intent": contract.interaction_intent,
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
