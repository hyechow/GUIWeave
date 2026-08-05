"""Production runtime bridge from compiled Python to Statement executors."""

from __future__ import annotations

import json
import multiprocessing
import traceback
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from gui_agent.core.run.contracts import (
    Acquire,
    Command,
    Interact,
    ObservationBinding,
    OutputSpec,
    Read,
    RunRecord,
    StatementInvocation,
)
from gui_agent.core.run.lookup_scope import is_lookup_scope
from gui_agent.core.run.statements.compute_kernel import json_value, normalize_table_rows
from gui_agent.core.filter_contract import (
    canonical_filter_field,
    compile_filter_predicates,
)
from gui_agent.core.schemas import (
    CollectionIntent,
    EventJournal,
    StatementContract,
    StatementOutcome,
)

from .sandbox import SAFE_BUILTINS, validate_code
from .models import (
    CurrentUI,
    field_projection,
    reach_postcondition,
    require_current_ui,
    structural_reach_state,
)


class CodingProgram(BaseModel):
    """Compiled restricted-Python planning artifact."""

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
        return value.token if isinstance(value, CurrentUI) else ""

    # Prefer the structured request keys that the report data panel already understands.
    if op == "reach":
        return {
            "goal": payload.get("goal"),
            "success": payload.get("success"),
            **({"target": payload.get("target")} if payload.get("target") is not None else {}),
        }
    if op == "commit":
        return {
            "goal": payload.get("goal"),
            **(dict(payload.get("inputs") or {}) if isinstance(payload.get("inputs"), dict) else {}),
            **({"state": _state_token(payload.get("state"))} if payload.get("state") else {}),
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
            "coverage": payload.get("coverage") or "complete",
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
            "field_types": payload.get("field_types") or {},
            "coverage": payload.get("coverage") or "complete",
            "requested_filters": payload.get("requested_filters") or {},
        }
    if op in {"open_target", "focus", "restore_source"}:
        return {
            "state": _state_token(payload.get("state")),
            "target": payload.get("target"),
            "fields": payload.get("fields") or [],
        }
    if op == "read":
        return {
            "state": _state_token(payload.get("state")),
            "fields": payload.get("fields") or [],
            "field_types": payload.get("field_types") or {},
        }
    if op == "command":
        return {
            "capability": payload.get("capability"),
            **(dict(payload.get("arguments") or {}) if isinstance(payload.get("arguments"), dict) else {}),
        }
    return {key: _clip(value) for key, value in payload.items()}


def _unique_target_url(target: Any) -> str:
    if not isinstance(target, dict):
        return ""
    urls = {
        str(value).strip()
        for key, value in target.items()
        if str(key).casefold().endswith(("url", "href"))
        and isinstance(value, str)
        and value.strip().casefold().startswith(("http://", "https://"))
    }
    return next(iter(urls)) if len(urls) == 1 else ""


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
        super().__init__(
            "; ".join(failures) or "coding program is not executable"
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
        self._call_seq = 0
        self._query_filters: dict[tuple[str, str], dict[str, Any]] = {}
        self._current_ui: CurrentUI | None = None

    def _call_id(self, op: str) -> str:
        self._call_seq += 1
        return f"{self._call_seq}:{op}"

    def _request(
        self,
        op: str,
        *,
        call_id: str,
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
            "call_id": call_id,
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
        *,
        entity: str,
        fields: list[str] | dict[str, str],
        filters: dict[str, Any] | None = None,
        coverage: str = "complete",
    ) -> list[dict[str, Any]]:
        field_names, field_types = field_projection(fields)
        state = require_current_ui(self._current_ui, entity=entity)
        call_id = self._call_id("query")
        # Private source session: locate, optionally constrain, then materialize.
        # The caller sees one ctx.query contract rather than executor phases.
        filters = dict(json_value(dict(filters or {})))
        filter_key = (state.token, canonical_filter_field(entity))
        desired_filters = compile_filter_predicates(filters)
        state_values = {
            key: value
            for key, value in state.postcondition.items()
            if canonical_filter_field(key) in desired_filters
        }
        state_filters = compile_filter_predicates(state_values)
        route_satisfies_filters = bool(desired_filters) and state_filters == desired_filters
        known_filters = (
            desired_filters
            if route_satisfies_filters
            else self._query_filters.get(filter_key)
        )
        needs_constrain = (
            bool(filters) if known_filters is None
            else desired_filters != known_filters
        )
        plan_steps = 3 if needs_constrain else 2
        source_fields = list(field_names)
        source_field_keys = {
            canonical_filter_field(field) for field in field_names
        }
        source_fields.extend(
            field for field in desired_filters
            if field not in source_field_keys and field not in state_filters
        )
        scope = self._request(
            "lookup",
            call_id=call_id,
            plan="query",
            plan_step=1,
            plan_steps=plan_steps,
            state=state,
            entity=entity,
            field="name",
            fallback="",
            required_fields=source_fields,
            coverage=coverage,
        )
        if needs_constrain:
            scope = self._request(
                "constrain",
                call_id=call_id,
                plan="query",
                plan_step=2,
                plan_steps=plan_steps,
                state=state,
                scope=scope,
                entity=entity,
                filters=filters,
            )
            self._query_filters[filter_key] = desired_filters
        return self._request(
            "acquire",
            call_id=call_id,
            plan="query",
            plan_step=plan_steps,
            plan_steps=plan_steps,
            state=state,
            scope=scope,
            fields=field_names,
            field_types=field_types,
            coverage=coverage,
            requested_filters=filters,
        )

    def read(
        self,
        *,
        target: Any = None,
        fields: list[str] | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        state = require_current_ui(self._current_ui)
        if fields is None:
            raise TypeError("ctx.read requires fields")
        field_names, field_types = field_projection(fields)
        call_id = self._call_id("read")
        source_state = state
        target = json_value(target) if target is not None else None
        target_url = _unique_target_url(target)
        steps = 4 if target_url else 3 if target is not None else 1
        if target is not None:
            if target_url:
                state = self._request(
                    "open_target",
                    call_id=call_id,
                    plan="read",
                    plan_step=1,
                    plan_steps=steps,
                    state=state,
                    target=target,
                    fields=field_names,
                    url=target_url,
                )
            state = self._request(
                "focus",
                call_id=call_id,
                plan="read",
                plan_step=2 if target_url else 1,
                plan_steps=steps,
                state=state,
                target=target,
                fields=field_names,
            )
        result = self._request(
            "read",
            call_id=call_id,
            plan="read",
            plan_step=steps - 1 if target is not None else steps,
            plan_steps=steps,
            state=state,
            fields=field_names,
            field_types=field_types,
        )
        if target is not None:
            self._request(
                "restore_source",
                call_id=call_id,
                plan="read",
                plan_step=steps,
                plan_steps=steps,
                state=source_state,
                current_state=state,
                target=target,
                fields=field_names,
            )
        return result

    def reach(
        self,
        goal: str,
        *,
        success: dict[str, Any],
        target: Any = None,
    ) -> None:
        normalized_success = json_value(success)
        normalized_target = json_value(target) if target is not None else None
        self._current_ui = require_current_ui(self._request(
            "reach",
            call_id=self._call_id("reach"),
            plan="reach",
            plan_step=1,
            plan_steps=1,
            goal=goal,
            success=dict(normalized_success),
            target=normalized_target,
        ))

    def commit(
        self,
        goal: str,
        *,
        target: Any = None,
        values: dict[str, Any],
    ) -> None:
        normalized_target = json_value(target) if target is not None else None
        normalized_values = json_value(values)
        inputs = {"target": normalized_target} if normalized_target is not None else {}
        state = (
            require_current_ui(self._current_ui, target=normalized_target)
            if normalized_target is not None
            else None
        )
        self._request(
            "commit",
            call_id=self._call_id("commit"),
            plan="commit",
            plan_step=1,
            plan_steps=1,
            goal=goal,
            inputs=inputs,
            state=state,
            values=dict(normalized_values),
        )
        self._current_ui = None

    def command(self, capability: str, **arguments: Any) -> Any:
        normalized_arguments = json_value(arguments)
        result = self._request(
            "command",
            call_id=self._call_id("command"),
            plan="command",
            plan_step=1,
            plan_steps=1,
            capability=capability,
            arguments=dict(normalized_arguments),
        )
        self._current_ui = None
        return result

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
    """Steppable runtime for reviewed Python and statement executors."""

    program: CodingProgram
    interpreter: CodingInterpreter
    current: StatementInvocation | None = None
    index: int = 0
    reply: str | None = None
    current_instance_id: str = ""
    current_coding_op: str = ""
    current_coding_payload: dict[str, Any] = field(default_factory=dict)
    current_coding_call_id: str = ""
    current_coding_plan: str = ""
    current_coding_plan_step: int = 0
    current_coding_plan_steps: int = 0
    _instance_seq: int = 0
    _statement_seq: int = 0
    _process: Any = None
    _connection: Any = None

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

    @classmethod
    def resume(
        cls,
        program: CodingProgram,
        journal: EventJournal,
    ) -> "CodingProgramRuntime":
        """Rebuild Python execution state from completed Statement receipts.

        The generated process is intentionally disposable. Restarting the exact
        reviewed source and returning persisted results to each yielded ``ctx.*``
        call reconstructs locals and branch decisions without serializing Python
        frames or touching the GUI.
        """
        events = journal.statement_outcomes
        runtime = cls.start(program)
        runtime._instance_seq = max(
            (
                cls._instance_number(
                    str(getattr(event, "statement_instance_id", "") or "")
                )
                for event in journal.events
            ),
            default=0,
        )
        try:
            for event in events:
                if not event.outcome.is_completed:
                    continue
                invocation = runtime.current
                if invocation is None:
                    raise ValueError(
                        "cannot resume coding program: journal has completed "
                        f"statement {event.statement_id!r} after program termination"
                    )
                if invocation.id != event.statement_id:
                    raise ValueError(
                        "cannot resume coding program: journal/program diverged at "
                        f"{event.statement_id!r}; program yielded {invocation.id!r}"
                    )
                runtime.current_instance_id = event.statement_instance_id
                runtime.send_outcome(event.outcome)
            if runtime.current is not None:
                terminal = {
                    event.statement_instance_id for event in journal.statement_outcomes
                }
                for turn in reversed(journal.turns):
                    statement_id = str(
                        getattr(turn.supervisor, "statement_id", "")
                        or getattr(turn.statement, "id", "")
                        or ""
                    )
                    if (
                        turn.statement_instance_id not in terminal
                        and statement_id == runtime.current.id
                    ):
                        runtime.current_instance_id = turn.statement_instance_id
                        break
            return runtime
        except Exception:
            runtime.close()
            raise

    @staticmethod
    def _instance_number(instance_id: str) -> int:
        prefix = str(instance_id or "").partition(":")[0]
        if prefix.startswith("i") and prefix[1:].isdigit():
            return int(prefix[1:])
        return 0

    @property
    def finished(self) -> bool:
        return self.current is None and self.reply is not None

    @staticmethod
    def adapt_outcome(outcome: StatementOutcome) -> StatementOutcome:
        """Normalize executor-specific terminal phases for the coding protocol."""
        if outcome.phase in {"completed", "failed"}:
            return outcome
        details = outcome.model_dump(
            exclude={"phase", "summary", "verification"},
        )
        return StatementOutcome.failed(outcome.summary, **details)

    def _id(self) -> str:
        self._statement_seq += 1
        return f"c{self._statement_seq}"

    @staticmethod
    def _state_context(state: CurrentUI) -> dict[str, Any]:
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
            coverage = str(payload.get("coverage") or "complete")
            state = require_current_ui(
                payload.get("state"),
                entity=entity,
            )
            request_text = (
                f"field={field_name!r}, fallback={fallback!r}, fields={required_fields!r}"
            )
            statement = Interact(
                id=statement_id,
                goal=(
                    f"Bind the structural collection surface for {entity!r}; ensure its "
                    f"available columns satisfy {request_text}. Collection identity is "
                    "independent of its current filters and number of records"
                ),
                success=(
                    f"One structural collection surface for {entity!r} is bound with the "
                    f"available columns required by {request_text}; row count is unrestricted "
                    "and the business context is unchanged"
                ),
                interaction_intent=CollectionIntent(
                    phase="locate",
                    entity=entity,
                    field=field_name,
                    fallback=fallback,
                    required_fields=required_fields,
                    coverage=coverage,
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
            state = require_current_ui(payload.get("state"))
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
                    required_fields=[
                        str(value)
                        for value in scope.get("available_fields") or []
                    ],
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
        if op == "open_target":
            state = require_current_ui(payload.get("state"))
            target = payload.get("target")
            target_url = str(payload.get("url") or "")
            if not target_url or target_url != _unique_target_url(target):
                raise ValueError("open_target requires one URL owned by its target")
            return StatementInvocation(
                statement=Command(
                    id=statement_id,
                    capability="open_url",
                    args={"url": target_url},
                ),
                task_goal=self.program.goal,
                inputs={
                    "ui_state": state.snapshot(),
                    "target": target,
                },
                args={
                    "url": target_url,
                    "ui_state_token": state.token,
                },
            )
        if op == "focus":
            state = require_current_ui(payload.get("state"))
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
        if op == "restore_source":
            state = require_current_ui(payload.get("state"))
            current_state = require_current_ui(payload.get("current_state"))
            expected = dict(state.postcondition)
            entity = str(expected.get("entity") or "")
            fields = [str(item) for item in expected.get("fields") or []]
            if not entity:
                raise ValueError("restore_source requires an entity-bearing source state")
            statement = Interact(
                id=statement_id,
                goal=(
                    "Restore the source UI state after inspecting the target; "
                    f"required source state: {expected!r}"
                ),
                success="The original source UI state is active again",
                expected_state=expected,
                interaction_intent=CollectionIntent(
                    phase="reach",
                    entity=entity,
                    required_fields=fields,
                ),
                persistence="immediate",
            )
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs={
                    "ui_state": state.snapshot(),
                    "current_ui_state": current_state.snapshot(),
                    "target": payload.get("target"),
                },
                args={"ui_state_token": state.token},
            )
        if op in {"reach", "commit"}:
            task = str(payload["goal"])
            values = dict(payload.get("values") or {}) if op == "commit" else {}
            success_text = f"The GUI task is complete: {task}"
            success: dict[str, Any] = {}
            if op == "reach":
                reach_state = reach_postcondition(payload.get("success"))
                if reach_state is None:
                    raise ValueError("ctx.reach success is not a structured state")
                # Target-bound reach: identity lives on target=row. Keep only
                # structural success (entity/fields + non-row-copied keys) so
                # complete gates do not demand list projection keys as detail fields.
                success = structural_reach_state(
                    reach_state, target=payload.get("target"),
                )
                success_text = "Every declared expected-state condition is established"
            state = None
            target = payload.get("target") if op == "reach" else (
                dict(payload.get("inputs") or {}).get("target")
            )
            if op == "commit" and target is not None:
                state = require_current_ui(payload.get("state"), target=target)
            statement = Interact(
                id=statement_id,
                goal=task,
                success=success_text,
                expected_state=success,
                required_values=values,
                persistence="explicit_commit" if op == "commit" else "immediate",
            )
            inputs = (
                {"target": target}
                if op == "reach" and target is not None
                else dict(payload.get("inputs") or {})
            )
            args: dict[str, Any] = {}
            if state is not None:
                inputs["ui_state"] = state.snapshot()
                args["ui_state_token"] = state.token
            return StatementInvocation(
                statement=statement,
                task_goal=self.program.goal,
                inputs=inputs,
                args=args,
            )
        if op == "acquire":
            scope = dict(payload.get("scope") or {})
            if not is_lookup_scope(scope):
                raise ValueError(
                    "internal query scope was not produced by the lookup statement"
                )
            state = require_current_ui(payload.get("state"))
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
                args={
                    "lookup_scope": scope,
                    "ui_state_token": state.token,
                    "field_types": dict(payload.get("field_types") or {}),
                    "requested_filters": dict(
                        payload.get("requested_filters") or {}
                    ),
                    "coverage": coverage,
                },
            )
        if op == "read":
            state = require_current_ui(payload.get("state"))
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
                inputs={"ui_state": state.snapshot()},
                args={
                    "ui_state_token": state.token,
                    "field_types": dict(payload.get("field_types") or {}),
                },
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
            call_id = str(message.get("call_id") or "")
            plan = str(message.get("plan") or op or "")
            plan_step = int(message.get("plan_step") or 1)
            plan_steps = int(message.get("plan_steps") or 1)
            try:
                self.current = self._invocation(op, payload)
                self.current_coding_op = op
                self.current_coding_payload = _report_coding_payload(op, payload)
                self.current_coding_call_id = call_id
                self.current_coding_plan = plan
                self.current_coding_plan_step = plan_step
                self.current_coding_plan_steps = plan_steps
            except Exception as exc:  # noqa: BLE001 - generated call contract
                self.current_coding_op = ""
                self.current_coding_payload = {}
                self.current_coding_call_id = ""
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
            self.current_coding_call_id = ""
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
        self.current_coding_call_id = ""
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
        outcome = self.adapt_outcome(outcome)
        invocation = self.current
        if invocation is None:
            raise RuntimeError("coding runtime has no active statement")
        instance_id = self.current_instance_id
        coding_op = self.current_coding_op
        coding_payload = dict(self.current_coding_payload)
        coding_call_id = self.current_coding_call_id
        issued_ui_state: CurrentUI | None = None
        if (
            outcome.is_completed
            and coding_op in {"reach", "open_target", "focus"}
        ):
            bound_target = (
                coding_payload.get("target")
                if coding_op == "reach"
                else invocation.inputs.get("target")
            )
            if coding_op == "focus":
                postcondition = {
                    "kind": "target_fields_available",
                    "target": invocation.inputs.get("target"),
                    "fields": list(coding_payload.get("fields") or []),
                }
                surface = "target_detail"
            elif coding_op == "open_target":
                postcondition = {
                    "kind": "target_open",
                    "target": invocation.inputs.get("target"),
                }
                surface = "target_detail"
            else:
                # reach: store structural postcondition (entity/fields); row
                # identity remains on CurrentUI.target for commit binding.
                raw_success = dict(coding_payload.get("success") or {})
                postcondition = structural_reach_state(
                    raw_success, target=bound_target,
                )
                surface = (
                    "target_detail" if bound_target is not None else "entity"
                )
            issued_ui_state = CurrentUI(
                token=f"{invocation.id}:state",
                postcondition=postcondition,
                observed_state=dict(outcome.outputs),
                target=bound_target,
                surface=surface,
            )
            coding_payload["produced_state"] = issued_ui_state.token
            if surface == "target_detail" and bound_target is not None:
                coding_payload["bound_target"] = bound_target
                coding_payload["surface"] = surface
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
            coding_call_id=coding_call_id,
            coding_plan=self.current_coding_plan,
            coding_plan_step=self.current_coding_plan_step,
            coding_plan_steps=self.current_coding_plan_steps,
        ))
        self.current_instance_id = ""
        self.current_coding_op = ""
        self.current_coding_payload = {}
        self.current_coding_call_id = ""
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
            value = outcome.outputs.get("scope")
            if (
                not is_lookup_scope(value)
                or str(value.get("entity") or "")
                != invocation.statement.interaction_intent.entity
            ):
                self._fail(
                    "constrain completed without a rebound collection scope"
                )
                return None
        elif issued_ui_state is not None:
            value = issued_ui_state
        elif isinstance(invocation.statement, Acquire):
            rows = outcome.outputs.get("rows", [])
            value = normalize_table_rows(
                rows if isinstance(rows, list) else [],
                dict(invocation.args.get("field_types") or {}),
            )
        elif isinstance(invocation.statement, Read):
            value = normalize_table_rows(
                [dict(outcome.outputs)],
                dict(invocation.args.get("field_types") or {}),
            )[0]
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
            "expected_state": contract.expected_state,
            "interaction_intent": contract.interaction_intent,
        })
        self.current = self.current.model_copy(update={"statement": statement})

def _render_return(value: Any) -> str:
    if value is None:
        return "Coding program completed"
    if isinstance(value, CurrentUI):
        value = value.snapshot()
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


__all__ = [
    "CodingCompileError",
    "CodingProgram",
    "CodingProgramRuntime",
    "program_from_plan",
]
