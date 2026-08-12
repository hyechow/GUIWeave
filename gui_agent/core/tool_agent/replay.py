"""Deterministic replay of reviewed Master programs against recorded GUI Workers.

Replay starts from an empty runtime data store. GUI Worker collection artifacts
are restored from the live run, while deterministic transforms and the frozen
Master control flow are executed again. It never resumes browser state or invokes
an LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from gui_agent.core.tool_agent.contracts import WorkerOutcome, WorkerSpec
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.orchestrator import (
    MasterTerminal,
    WorkerOrchestrationContext,
    execute_master_program,
)


@dataclass(frozen=True)
class RecordedGuiWorker:
    worker_id: str
    spec: WorkerSpec
    outcome: WorkerOutcome
    value: Any = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecordedGuiWorker":
        return cls(
            worker_id=str(value["worker_id"]),
            spec=WorkerSpec.model_validate(value["spec"]),
            outcome=WorkerOutcome.model_validate(value["outcome"]),
            value=value.get("value"),
        )


@dataclass(frozen=True)
class RecordedProgram:
    source: str
    execution: int
    expected_kind: Literal["terminal", "error"]
    expected_phase: str = ""


@dataclass(frozen=True)
class RecordedContext:
    """External responses and expected terminal result from one live run."""

    gui_workers: dict[str, tuple[RecordedGuiWorker, ...]]
    expected_phase: str
    expected_output: Any = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecordedContext":
        workers = [
            RecordedGuiWorker.from_dict(item)
            for item in (value.get("gui_workers") or [])
        ]
        grouped: dict[str, list[RecordedGuiWorker]] = {}
        for item in workers:
            grouped.setdefault(item.worker_id, []).append(item)
        return cls(
            gui_workers={key: tuple(items) for key, items in grouped.items()},
            expected_phase=str(value.get("expected_phase") or ""),
            expected_output=value.get("expected_output"),
        )


@dataclass(frozen=True)
class RecordedRun:
    programs: tuple[RecordedProgram, ...]
    context: RecordedContext
    platform_time: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReplayResult:
    status: Literal["passed", "failed", "unavailable"]
    summary: str
    phase: str = ""
    output: Any = None
    expected_phase: str = ""
    expected_output: Any = None
    program_count: int = 0
    gui_worker_count: int = 0
    trace: tuple[dict[str, Any], ...] = ()
    error: str = ""
    platform_time: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": self.summary,
            "phase": self.phase,
            "output": self.output,
            "expected_phase": self.expected_phase,
            "expected_output": self.expected_output,
            "program_count": self.program_count,
            "gui_worker_count": self.gui_worker_count,
            "trace": list(self.trace),
            "error": self.error,
            "platform_time": self.platform_time,
            "uses_browser": False,
            "uses_llm": False,
        }


def _recorded_programs(events: list[dict[str, Any]]) -> tuple[RecordedProgram, ...]:
    programs: list[RecordedProgram] = []
    executions = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("event") == "master_program_execution_started"
    ]
    if not executions:
        executions = [
            (index, event)
            for index, event in enumerate(events)
            if event.get("event") == "master_program_generated"
        ]
    for ordinal, (index, event) in enumerate(executions):
        end = executions[ordinal + 1][0] if ordinal + 1 < len(executions) else len(events)
        conclusion = next(
            (
                item
                for item in events[index + 1:end]
                if item.get("event") in {"master_program_completed", "master_program_error"}
            ),
            None,
        )
        if conclusion is None:
            continue
        is_error = conclusion.get("event") == "master_program_error"
        programs.append(RecordedProgram(
            source=str(event.get("source") or ""),
            execution=int(event.get("execution") or event.get("generation") or ordinal + 1),
            expected_kind="error" if is_error else "terminal",
            expected_phase="" if is_error else str(conclusion.get("phase") or ""),
        ))
    return tuple(programs)


def load_recorded_run(run_dir: Path) -> RecordedRun:
    """Load the minimal replay fixture from normal Tool Agent run artifacts."""
    trace_path = run_dir / "tool_agent_trace.json"
    store_path = run_dir / "tool_agent_data_store.json"
    raw = json.loads(trace_path.read_text(encoding="utf-8"))
    store = json.loads(store_path.read_text(encoding="utf-8"))
    events = [item for item in (raw.get("trace") or []) if isinstance(item, dict)]
    values = dict(store.get("values") or {})

    dispatches = [
        event
        for event in events
        if event.get("event") == "master_worker_dispatch"
        and event.get("kind") == "gui"
        and event.get("worker_id")
    ]
    results_by_id: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not (
            event.get("event") == "master_worker_result"
            and event.get("kind") == "gui"
            and event.get("worker_id")
        ):
            continue
        results_by_id.setdefault(str(event["worker_id"]), []).append(event)
    result_offsets: dict[str, int] = {}
    workers: list[RecordedGuiWorker] = []
    for dispatch in dispatches:
        worker_id = str(dispatch.get("worker_id") or "")
        offset = result_offsets.get(worker_id, 0)
        candidates = results_by_id.get(worker_id) or []
        result = candidates[offset] if offset < len(candidates) else None
        result_offsets[worker_id] = offset + 1
        if result is None:
            continue
        spec = WorkerSpec.model_validate(dispatch.get("spec") or {})
        outcome = WorkerOutcome.model_validate(result.get("outcome") or {})
        recorded_value = None
        if outcome.phase == "completed" and outcome.collection_ref is not None:
            if outcome.collection_ref.ref not in values:
                raise ValueError(
                    f"recording has no private value for {outcome.collection_ref.ref!r}"
                )
            recorded_value = values[outcome.collection_ref.ref]
        workers.append(RecordedGuiWorker(worker_id, spec, outcome, recorded_value))

    grouped_workers: dict[str, list[RecordedGuiWorker]] = {}
    for worker in workers:
        grouped_workers.setdefault(worker.worker_id, []).append(worker)

    programs = _recorded_programs(events)
    if not programs:
        raise ValueError("recording has no concluded reviewed Master program")
    return RecordedRun(
        programs=programs,
        context=RecordedContext(
            gui_workers={key: tuple(items) for key, items in grouped_workers.items()},
            expected_phase=str(raw.get("phase") or ""),
            expected_output=raw.get("output"),
        ),
        platform_time=(
            dict(raw.get("platform_time") or {})
            or next(
                (
                    dict(event.get("platform_time") or {})
                    for event in events
                    if event.get("event") == "runtime_started"
                    and event.get("platform_time")
                ),
                None,
            )
        ),
    )


def _replay_programs(
    programs: tuple[RecordedProgram, ...],
    recording: RecordedContext,
) -> ReplayResult:
    data_store = RuntimeDataStore()
    trace: list[dict[str, Any]] = []
    fixture_errors: list[str] = []
    worker_offsets: dict[str, int] = {}

    def record(event: str, **payload: Any) -> None:
        trace.append({"event": event, **payload})

    def run_gui_worker(worker_id: str, spec: WorkerSpec) -> WorkerOutcome:
        attempts = recording.gui_workers.get(worker_id) or ()
        offset = worker_offsets.get(worker_id, 0)
        recorded = attempts[offset] if offset < len(attempts) else None
        if recorded is None:
            message = f"no recorded GUI Worker outcome for {worker_id!r}"
            fixture_errors.append(message)
            raise KeyError(message)
        if recorded.spec != spec:
            message = (
                f"recorded GUI Worker {worker_id!r} specification does not match replay"
            )
            fixture_errors.append(message)
            raise ValueError(message)
        outcome = recorded.outcome
        worker_offsets[worker_id] = offset + 1
        if outcome.phase == "failed":
            return outcome
        if outcome.collection_ref is not None:
            if not isinstance(recorded.value, list):
                raise ValueError(f"recorded collection for {worker_id!r} is not a row list")
            data_store.restore_collection(outcome.collection_ref, recorded.value)
        return WorkerOutcome(
            phase="completed",
            summary=outcome.summary,
            collection_ref=outcome.collection_ref,
            steps=outcome.steps,
        )

    orchestration = WorkerOrchestrationContext(
        data_store=data_store,
        run_gui_worker=run_gui_worker,
        trace=record,
    )
    terminal: MasterTerminal | None = None
    for program in programs:
        record("replay_program_started", execution=program.execution)
        execution = execute_master_program(program.source, orchestration)
        if fixture_errors:
            return _failed_result(
                recording,
                programs,
                trace,
                fixture_errors[-1],
                terminal=execution.terminal,
                data_store=data_store,
            )
        if program.expected_kind == "error":
            if not execution.error:
                return _failed_result(
                    recording,
                    programs,
                    trace,
                    f"execution {program.execution} no longer reproduces its execution error",
                )
            record(
                "replay_program_error_reproduced",
                execution=program.execution,
                error=execution.error,
            )
            continue
        if execution.error:
            return _failed_result(
                recording,
                programs,
                trace,
                f"execution {program.execution} failed during replay: {execution.error}",
            )
        terminal = execution.terminal
        assert terminal is not None
        record(
            "replay_program_completed",
            execution=program.execution,
            phase=terminal.phase,
            result_ref=terminal.result_ref,
        )
        if terminal.phase != program.expected_phase:
            return _failed_result(
                recording,
                programs,
                trace,
                f"execution {program.execution} phase changed from "
                f"{program.expected_phase!r} to {terminal.phase!r}",
                terminal=terminal,
                data_store=data_store,
            )

    if terminal is None:
        return _failed_result(recording, programs, trace, "replay produced no terminal result")
    output = (
        data_store.result_value(terminal.result_ref)
        if terminal.phase == "completed" and terminal.result_ref
        else None
    )
    if terminal.phase != recording.expected_phase:
        return _failed_result(
            recording,
            programs,
            trace,
            f"final phase changed from {recording.expected_phase!r} to {terminal.phase!r}",
            terminal=terminal,
            output=output,
        )
    if output != recording.expected_output:
        return _failed_result(
            recording,
            programs,
            trace,
            "final output differs from the recorded live run",
            terminal=terminal,
            output=output,
        )
    return ReplayResult(
        status="passed",
        summary=(
            f"Deterministic replay passed: {len(programs)} frozen-program execution(s), "
            f"{len(recording.gui_workers)} recorded GUI Worker(s)."
        ),
        phase=terminal.phase,
        output=output,
        expected_phase=recording.expected_phase,
        expected_output=recording.expected_output,
        program_count=len(programs),
        gui_worker_count=len(recording.gui_workers),
        trace=tuple(trace),
    )


def _failed_result(
    recording: RecordedContext,
    programs: tuple[RecordedProgram, ...],
    trace: list[dict[str, Any]],
    error: str,
    *,
    terminal: MasterTerminal | None = None,
    data_store: RuntimeDataStore | None = None,
    output: Any = None,
) -> ReplayResult:
    if output is None and terminal is not None and terminal.phase == "completed" and data_store:
        output = data_store.result_value(terminal.result_ref)
    return ReplayResult(
        status="failed",
        summary=f"Deterministic replay failed: {error}",
        phase=terminal.phase if terminal else "",
        output=output,
        expected_phase=recording.expected_phase,
        expected_output=recording.expected_output,
        program_count=len(programs),
        gui_worker_count=len(recording.gui_workers),
        trace=tuple(trace),
        error=error,
    )


def replay_program(source: str, recording: RecordedContext) -> ReplayResult:
    """Rerun one reviewed program from the beginning against recorded GUI results."""
    program = RecordedProgram(
        source=source,
        execution=1,
        expected_kind="terminal",
        expected_phase=recording.expected_phase,
    )
    return _replay_programs((program,), recording)


def replay_recorded_run(recording: RecordedRun) -> ReplayResult:
    """Rerun all executions of the frozen reviewed program in original order."""
    return replace(
        _replay_programs(recording.programs, recording.context),
        platform_time=recording.platform_time,
    )


def replay_run_directory(run_dir: Path) -> ReplayResult:
    return replay_recorded_run(load_recorded_run(run_dir))


def write_replay_artifact(run_dir: Path) -> ReplayResult:
    """Replay a finished run and persist an inspectable verdict beside its trace."""
    try:
        result = replay_run_directory(run_dir)
    except Exception as exc:  # noqa: BLE001 - unavailable replay is a reportable artifact
        result = ReplayResult(
            status="unavailable",
            summary=f"Deterministic replay unavailable: {type(exc).__name__}: {exc}",
            error=f"{type(exc).__name__}: {exc}",
        )
    (run_dir / "tool_agent_replay.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


__all__ = [
    "RecordedContext",
    "RecordedGuiWorker",
    "RecordedProgram",
    "RecordedRun",
    "ReplayResult",
    "load_recorded_run",
    "replay_program",
    "replay_recorded_run",
    "replay_run_directory",
    "write_replay_artifact",
]
