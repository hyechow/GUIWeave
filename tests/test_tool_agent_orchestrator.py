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


ROW_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "number"}},
    "required": ["amount"],
    "additionalProperties": False,
}

GUI_CALL = f"""
ctx.gui_worker(
    worker_id="collect_records",
    profile="collector",
    goal="Collect the records needed by the task",
    success_criteria=["The requested records have complete coverage"],
    data_requirements=[{{
        "id": "records",
        "description": "Requested records",
        "row_schema": {ROW_SCHEMA!r},
    }}],
    actions=[{{
        "name": "reveal_more",
        "capability": "scroll",
        "description": "Reveal more records when needed",
        "fixed_args": {{"direction": "down", "target_area": "main_content"}},
        "exposed_args": ["amount"],
    }}],
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


def _put_complete_collection(store: RuntimeDataStore, rows: list[dict]):
    _, descriptor, _ = store.put_chunk(
        requirement_id="records",
        frame_id="frame:1",
        provider="structured",
        rows=rows,
        row_schema=ROW_SCHEMA,
        coverage={
            "source_scope": "structured_surface",
            "scope_status": "met",
            "traversal_type": "static",
            "partial": False,
            "total_records": len(rows),
        },
    )
    return descriptor


def test_master_review_rejects_gui_micro_actions() -> None:
    diagnostics = validate_master_source(
        "def run(ctx):\n    ctx.tap(100, 200)\n    ctx.fail('invalid program')"
    )

    assert any(item.code == "UNKNOWN_CTX_API" for item in diagnostics)


def test_master_review_requires_ref_strings_from_descriptors() -> None:
    descriptor_access = validate_master_source(
        "def run(ctx):\n"
        "    outcome = ctx.worker_result('collect')\n"
        "    ctx.finish(outcome['collection_ref'])"
    )
    attribute_access = validate_master_source(
        "def run(ctx):\n"
        "    outcome = ctx.worker_result('collect')\n"
        "    ctx.finish(outcome['collection_ref'].ref)"
    )

    assert any(item.code == "REF_VALUE_REQUIRED" for item in descriptor_access)
    assert any(item.code == "ATTRIBUTE_ACCESS" for item in attribute_access)


def test_master_review_rejects_transform_fields_missing_from_worker_schema() -> None:
    source = _program(
        '''
reviews = ctx.gui_worker(
    worker_id="collect_reviews",
    profile="collector",
    goal="Collect the requested reviews",
    success_criteria=["All requested review data is collected"],
    data_requirements=[{
        "id": "reviews",
        "description": "Requested reviews",
        "row_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    }],
    actions=[{
        "name": "collect_visible_reviews",
        "capability": "scroll",
        "description": "Collect all review rows",
        "fixed_args": {"direction": "down"},
    }],
)
result = ctx.transform(
    transform_id="filter_reviews",
    inputs=[reviews["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    rows = inputs[0]\\n    return [row.get('rating') for row in rows]",
    result_schema={"type": "array", "items": {"type": "number"}},
)
ctx.finish(result["ref"])
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "TRANSFORM_INPUT_SCHEMA" for item in diagnostics)
    assert any("rating" in item.message for item in diagnostics)


def test_master_compiler_regenerates_only_during_static_review() -> None:
    invalid = "def run(ctx):\n    ctx.tap(1, 2)\n    ctx.fail('bad')"
    valid = "def run(ctx):\n    ctx.fail('No safe execution path')"
    llm = _SequenceLLM(invalid, valid)
    events = []

    program = compile_master_program(
        llm=llm,
        system_prompt="Compile a program.",
        task_context={"goal": "test"},
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert program.source == valid
    assert program.attempts == 2
    assert len(llm.invocations) == 2
    assert llm.bind_kwargs["extra_body"] == {"enable_thinking": False}
    assert events[0][1]["diagnostics"]
    assert events[1][1]["diagnostics"] == []


def test_coding_master_orchestrates_gui_then_runtime_transform() -> None:
    store = RuntimeDataStore()
    gui_calls = []
    trace = []

    def run_gui_worker(worker_id, spec):
        assert worker_id == "collect_records"
        gui_calls.append(spec)
        descriptor = _put_complete_collection(store, [{"amount": 2}, {"amount": 3}])
        return WorkerOutcome(
            phase="completed",
            summary="Collected records",
            collection_ref=descriptor,
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
    ctx.fail(records["summary"])
total = ctx.transform(
    transform_id="sum_amounts",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return sum(row['amount'] for row in inputs[0])",
    result_schema={{"type": "number"}},
)
ctx.finish(total["ref"])
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
        "transform_started",
        "transform_completed",
    ]


def test_reexecuted_frozen_program_reuses_completed_gui_worker_and_transform() -> None:
    store = RuntimeDataStore()
    gui_calls = []
    trace = []

    def run_gui_worker(worker_id, spec):
        gui_calls.append((worker_id, spec))
        return WorkerOutcome(
            phase="completed",
            summary="Collected once",
            collection_ref=_put_complete_collection(store, []),
            steps=1,
        )

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=run_gui_worker,
        trace=lambda event, **payload: trace.append({"event": event, **payload}),
    )
    body = f"""
records = {GUI_CALL}
result = ctx.transform(
    transform_id="count_records",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return len(inputs[0])",
    result_schema={{"type": "integer"}},
)
ctx.finish(result["ref"])
""".strip()
    source = _program(body)

    first = execute_master_program(source, ctx)
    second = execute_master_program(source, ctx)

    assert first.ok and second.ok
    assert len(gui_calls) == 1
    assert any(item["event"] == "master_worker_reuse" for item in trace)
    assert any(item["event"] == "transform_reused" for item in trace)
    assert ctx.worker_result("collect_records")["phase"] == "completed"


def test_legacy_program_replan_is_sealed_as_failure_without_reexecution() -> None:
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
ctx.fail("unreachable")
""".strip()
    )

    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert execution.terminal.phase == "failed"
    assert execution.terminal.summary == "Unexpected access gate"


def test_operator_can_materialize_a_control_flow_result_without_fake_data() -> None:
    store = RuntimeDataStore()

    def run_gui_worker(worker_id, spec):
        assert worker_id == "update_setting"
        assert spec.profile == "operator"
        return WorkerOutcome(phase="completed", summary="Setting visibly updated", steps=2)

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=run_gui_worker,
        trace=lambda *args, **kwargs: None,
    )
    source = _program(
        """
updated = ctx.gui_worker(
    worker_id="update_setting",
    profile="operator",
    goal="Update the requested setting",
    success_criteria=["The requested state is visibly confirmed"],
    data_requirements=[],
    actions=[{
        "name": "activate_setting",
        "capability": "tap",
        "description": "Activate the visible requested setting",
    }],
)
if updated["phase"] != "completed":
    ctx.fail(updated["summary"])
result = ctx.transform(
    transform_id="operator_success",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={"type": "boolean"},
)
ctx.finish(result["ref"])
""".strip()
    )

    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert store.result_value(execution.terminal.result_ref) is True
