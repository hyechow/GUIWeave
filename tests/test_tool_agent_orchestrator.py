from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import WorkerOutcome
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.orchestrator import (
    WorkerOrchestrationContext,
    compile_master_program,
    execute_master_program,
    validate_master_source,
)


GUI_CALL = """
ctx.gui_worker(
    worker_id="collect_records",
    profile="collector",
    goal="Collect the records needed by the task",
    success_criteria=["The requested records have complete coverage"],
    data_requirements=[],
    actions=[{
        "name": "reveal_more",
        "capability": "scroll",
        "description": "Reveal more records when needed",
        "fixed_args": {"direction": "down", "target_area": "main_content"},
        "exposed_args": ["amount"],
    }],
    result_schema={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
            "additionalProperties": False,
        },
    },
    max_steps=8,
)
""".strip()


def _program(body: str) -> str:
    return "def run(ctx):\n" + "\n".join(f"    {line}" for line in body.splitlines())


class _SequenceLLM:
    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.invocations = []
        self.bind_kwargs = {}

    def bind(self, **kwargs):
        self.bind_kwargs = kwargs
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return SimpleNamespace(content=self.responses.pop(0))


def test_master_review_rejects_gui_micro_actions() -> None:
    diagnostics = validate_master_source(
        "def run(ctx):\n    ctx.tap(100, 200)\n    ctx.fail('invalid program')"
    )

    assert any(item.code == "UNKNOWN_CTX_API" for item in diagnostics)


def test_master_review_requires_ref_strings_from_outcome_dicts() -> None:
    descriptor_access = validate_master_source(
        "def run(ctx):\n"
        "    outcome = ctx.worker_result('collect')\n"
        "    ctx.finish(outcome['result_ref'])"
    )
    attribute_access = validate_master_source(
        "def run(ctx):\n"
        "    outcome = ctx.worker_result('collect')\n"
        "    ctx.finish(outcome['result_ref'].ref)"
    )

    assert any(item.code == "REF_VALUE_REQUIRED" for item in descriptor_access)
    assert any(item.code == "ATTRIBUTE_ACCESS" for item in attribute_access)


def test_master_compiler_regenerates_the_whole_reviewed_program() -> None:
    invalid = "def run(ctx):\n    ctx.tap(1, 2)\n    ctx.fail('bad')"
    valid = "def run(ctx):\n    ctx.fail('No safe execution path')"
    llm = _SequenceLLM(invalid, valid)
    events = []

    program = compile_master_program(
        llm=llm,
        system_prompt="Compile a program.",
        task_context={"goal": "test"},
        execution_history=[],
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert program.source == valid
    assert program.attempts == 2
    assert len(llm.invocations) == 2
    assert llm.bind_kwargs["extra_body"] == {"enable_thinking": False}
    assert events[0][1]["diagnostics"]
    assert events[1][1]["diagnostics"] == []


def test_coding_master_orchestrates_gui_then_data_worker() -> None:
    store = RuntimeDataStore()
    gui_calls = []
    trace = []

    def run_gui_worker(worker_id, spec):
        assert worker_id == "collect_records"
        gui_calls.append(spec)
        descriptor = store.put_result(
            [{"amount": 2}, {"amount": 3}],
            spec.result_schema,
            summary="Collected records",
        )
        return WorkerOutcome(
            phase="completed",
            summary="Collected records",
            result_ref=descriptor,
            steps=3,
        )

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=run_gui_worker,
        trace=lambda event, **payload: trace.append({"event": event, **payload}),
    )
    source = _program(
        f"""
records = {GUI_CALL}
if records["phase"] != "completed":
    ctx.replan(records["summary"])
total = ctx.data_worker(
    worker_id="sum_amounts",
    goal="Sum the collected amounts deterministically",
    inputs=[records["result_ref"]["ref"]],
    source="def transform(inputs):\\n    return sum(row['amount'] for row in inputs[0])",
    result_schema={{"type": "number"}},
)
if total["phase"] != "completed":
    ctx.replan(total["summary"])
ctx.finish(total["result_ref"]["ref"])
""".strip()
    )

    assert validate_master_source(source) == []
    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert execution.terminal.phase == "completed"
    assert store.result_value(execution.terminal.result_ref) == 5
    assert len(gui_calls) == 1
    assert gui_calls[0].profile == "collector"
    assert [item["event"] for item in trace] == [
        "master_worker_dispatch",
        "master_worker_result",
        "data_worker_start",
        "data_worker_complete",
    ]


def test_recompiled_program_reuses_completed_gui_worker_by_stable_id() -> None:
    store = RuntimeDataStore()
    gui_calls = []
    trace = []

    def run_gui_worker(worker_id, spec):
        assert worker_id == "collect_records"
        gui_calls.append(spec)
        descriptor = store.put_result([], spec.result_schema, summary="Collected once")
        return WorkerOutcome(
            phase="completed",
            summary="Collected once",
            result_ref=descriptor,
            steps=1,
        )

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=run_gui_worker,
        trace=lambda event, **payload: trace.append({"event": event, **payload}),
    )
    first = _program(f'records = {GUI_CALL}\nctx.finish("result:missing")')
    second = _program(
        f'records = {GUI_CALL}\nctx.finish(records["result_ref"]["ref"])'
    )

    failed = execute_master_program(first, ctx)
    completed = execute_master_program(second, ctx)

    assert not failed.ok
    assert "unknown ResultRef" in failed.error
    assert completed.ok
    assert len(gui_calls) == 1
    assert any(item["event"] == "master_worker_reuse" for item in trace)
    assert ctx.history_for_model()[0]["worker_id"] == "collect_records"


def test_program_can_request_model_replanning_from_typed_worker_failure() -> None:
    store = RuntimeDataStore()

    def fail_gui_worker(worker_id, spec):
        assert worker_id == "collect_records"
        del spec
        return WorkerOutcome(phase="failed", summary="Unexpected access gate", steps=1)

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=fail_gui_worker,
        trace=lambda *args, **kwargs: None,
    )
    source = _program(
        f"""
records = {GUI_CALL}
if records["phase"] != "completed":
    ctx.replan(records["summary"])
ctx.finish(records["result_ref"]["ref"])
""".strip()
    )

    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert execution.terminal.phase == "replan"
    assert execution.terminal.summary == "Unexpected access gate"
