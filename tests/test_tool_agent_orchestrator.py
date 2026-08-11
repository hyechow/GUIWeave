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
        "    ctx.finish(outcome['collection_ref'], effect='data')"
    )
    attribute_access = validate_master_source(
        "def run(ctx):\n"
        "    outcome = ctx.worker_result('collect')\n"
        "    ctx.finish(outcome['collection_ref'].ref, effect='data')"
    )

    assert any(item.code == "REF_VALUE_REQUIRED" for item in descriptor_access)
    assert any(item.code == "ATTRIBUTE_ACCESS" for item in attribute_access)


def test_master_review_requires_explicit_terminal_effect() -> None:
    diagnostics = validate_master_source(
        "def run(ctx):\n"
        "    result = ctx.transform(transform_id='done', inputs=[], "
        "source='def transform(inputs):\\n    return True', "
        "result_schema={'type': 'boolean'})\n"
        "    ctx.finish(result['ref'])"
    )

    assert any(item.code == "FINISH_EFFECT" for item in diagnostics)


def test_live_709_review_rejects_transform_descriptor_passed_to_finish() -> None:
    source = _program(
        '''
final_ref = ctx.transform(
    transform_id="confirm_report_rendered",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={"type": "boolean"},
)
ctx.finish(final_ref, effect="ui_state")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "REF_VALUE_REQUIRED" for item in diagnostics)
    assert any("final_ref['ref']" in item.message for item in diagnostics)


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
ctx.finish(result["ref"], effect="data")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "TRANSFORM_INPUT_SCHEMA" for item in diagnostics)
    assert any("rating" in item.message for item in diagnostics)


def test_master_review_tracks_collection_ref_alias_into_transform_schema() -> None:
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
reviews_ref = reviews["collection_ref"]["ref"]
result = ctx.transform(
    transform_id="filter_reviews",
    inputs=[reviews_ref],
    source="def transform(inputs):\\n    return [row.get('rating') for row in inputs[0]]",
    result_schema={"type": "array", "items": {"type": "number"}},
)
ctx.finish(result["ref"], effect="data")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert not any(item.code == "COLLECTOR_RESULT_UNUSED" for item in diagnostics)
    assert any(item.code == "TRANSFORM_INPUT_SCHEMA" for item in diagnostics)
    assert any("rating" in item.message for item in diagnostics)


def test_master_review_accepts_collection_ref_alias_consumed_by_transform() -> None:
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
reviews_ref = reviews["collection_ref"]["ref"]
result = ctx.transform(
    transform_id="project_reviews",
    inputs=[reviews_ref],
    source="def transform(inputs):\\n    return [{'title': row['title']} for row in inputs[0]]",
    result_schema={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
)
ctx.finish(result["ref"], effect="data")
'''.strip()
    )

    assert validate_master_source(source) == []


def test_master_review_rejects_computed_result_not_routed_to_next_gui_worker() -> None:
    source = _program(
        '''
computed = ctx.transform(
    transform_id="compute_target_text",
    inputs=[],
    source="def transform(inputs):\\n    return {'text': 'computed value'}",
    result_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
updated = ctx.gui_worker(
    worker_id="apply_target_text",
    profile="operator",
    goal="Apply the computed target text",
    success_criteria=["The computed target text is saved"],
    data_requirements=[],
    actions=[{
        "name": "enter_target_text",
        "capability": "type",
        "description": "Enter the computed target text",
        "exposed_args": ["text"],
    }],
)
if updated["phase"] != "completed":
    ctx.fail(updated["summary"])
ctx.finish(computed["ref"], effect="mutation")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "RESULT_REF_UNROUTED" for item in diagnostics)


def test_master_review_rejects_collector_used_only_as_navigation_handle() -> None:
    source = _program(
        '''
located = ctx.gui_worker(
    worker_id="locate_record",
    profile="collector",
    goal="Locate the record and open its editor",
    success_criteria=["The record editor is open"],
    data_requirements=[{
        "id": "record",
        "description": "The record action handle",
        "row_schema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
        "field_types": {"action": "text"},
    }],
    actions=[{
        "name": "open_record",
        "capability": "tap",
        "description": "Open the matching record",
    }],
)
if located["phase"] != "completed":
    ctx.fail(located["summary"])
updated = ctx.gui_worker(
    worker_id="update_record",
    profile="operator",
    goal="Update the currently open record",
    success_criteria=["The record is saved"],
    data_requirements=[],
    actions=[{
        "name": "save_record",
        "capability": "tap",
        "description": "Save the record",
    }],
)
done = ctx.transform(
    transform_id="done",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={"type": "boolean"},
)
ctx.finish(done["ref"], effect="mutation")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "COLLECTOR_RESULT_UNUSED" for item in diagnostics)


def test_master_review_rejects_private_array_as_gui_worker_iteration_source() -> None:
    source = _program(
        '''
rows = ctx.transform(
    transform_id="prepare_targets",
    inputs=[],
    source="def transform(inputs):\\n    return [{'id': '1'}, {'id': '2'}]",
    result_schema={
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
)
updated = ctx.gui_worker(
    worker_id="update_targets",
    profile="operator",
    goal="Update every target",
    success_criteria=["Every target is saved"],
    input_refs={"targets": rows["ref"]},
    data_requirements=[],
    actions=[{
        "name": "locate_target",
        "capability": "type",
        "description": "Locate one target",
        "input_args": {"text": {"input": "targets", "path": ["id"]}},
    }],
)
if updated["phase"] != "completed":
    ctx.fail(updated["summary"])
done = ctx.transform(
    transform_id="done",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={"type": "boolean"},
)
ctx.finish(done["ref"], effect="mutation")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "WORKER_ARRAY_INPUT_UNSUPPORTED" for item in diagnostics
    )


def test_master_review_normalizes_lossless_task544_schema_variants() -> None:
    source = _program(
        '''
reviews = ctx.gui_worker(
    worker_id="collect_reviews",
    profile="collector",
    goal="Collect reviews for one product",
    success_criteria="All matching review ratings are collected.",
    data_requirements=[{
        "id": "reviews",
        "description": "Matching review ratings",
        "row_schema": {
            "type": "object",
            "properties": {
                "rating": {"type": "number"},
                "product": {"type": "string"},
            },
            "required": ["rating", "product"],
        },
        "field_sources": {"rating": "Detailed Rating", "product": "Product"},
        "field_types": {"rating": "number", "product": "text"},
        "filters": {"product": "Target Product"},
    }],
    actions=[{
        "name": "filter_product",
        "capability": "type",
        "description": "Filter by product",
        "fixed_args": {"text": "Target Product"},
    }],
    max_steps=20,
)
if reviews["phase"] != "completed":
    ctx.fail(reviews["summary"])
computed = ctx.transform(
    transform_id="compute_text",
    inputs=[reviews["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return {'text': str(len(inputs[0]))}",
    result_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
updated = ctx.gui_worker(
    worker_id="update_product",
    profile="operator",
    goal="Apply the computed text to the target product",
    success_criteria="The product is saved successfully.",
    input_refs={"computed": computed["ref"]},
    data_requirements=[],
    actions=[
        {
            "name": "search_product",
            "capability": "type",
            "description": "Search the product grid",
            "fixed_args": {"text": "Target Product"},
        },
        {
            "name": "enter_text",
            "capability": "type",
            "description": "Enter the computed text",
            "input_args": {"text": {"input": "computed", "path": ["text"]}},
        },
    ],
    max_steps=20,
)
if updated["phase"] != "completed":
    ctx.fail(updated["summary"])
ctx.finish(computed["ref"], effect="mutation")
'''.strip()
    )

    assert validate_master_source(source) == []


def test_master_review_defaults_missing_operator_data_requirements_only() -> None:
    operator = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="save_target",
    profile="operator",
    goal="Save the target",
    success_criteria=["The target is saved"],
    actions=[{
        "name": "save",
        "capability": "tap",
        "description": "Save the target",
    }],
)
if outcome["phase"] != "completed":
    ctx.fail(outcome["summary"])
done = ctx.transform(
    transform_id="done",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={"type": "boolean"},
)
ctx.finish(done["ref"], effect="mutation")
'''.strip()
    )
    collector = operator.replace('profile="operator"', 'profile="collector"')

    assert validate_master_source(operator) == []
    assert any(
        item.code == "GUI_WORKER_LITERAL"
        for item in validate_master_source(collector)
    )


def test_master_review_accepts_explicit_null_input_refs_as_empty() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="save_target",
    profile="operator",
    goal="Save the target",
    success_criteria=["The target is saved"],
    input_refs=None,
    data_requirements=[],
    actions=[{
        "name": "save",
        "capability": "tap",
        "description": "Save the target",
    }],
)
if outcome["phase"] != "completed":
    ctx.fail(outcome["summary"])
done = ctx.transform(
    transform_id="done",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={"type": "boolean"},
)
ctx.finish(done["ref"], effect="mutation")
'''.strip()
    )

    assert validate_master_source(source) == []


def test_result_ref_binds_exact_runtime_value_to_worker_action() -> None:
    store = RuntimeDataStore()
    specs = []
    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=lambda _worker_id, spec: (
            specs.append(spec)
            or WorkerOutcome(phase="completed", summary="Saved", steps=1)
        ),
        trace=lambda _event, **_payload: None,
    )
    source = _program(
        '''
raw = ctx.transform(
    transform_id="raw_target_text",
    inputs=[],
    source="def transform(inputs):\\n    return {'value': 'computed value'}",
    result_schema={
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    },
)
computed = ctx.transform(
    transform_id="compute_target_text",
    inputs=[raw["ref"]],
    source="def transform(inputs):\\n    return {'text': inputs[0]['value']}",
    result_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
)
updated = ctx.gui_worker(
    worker_id="apply_target_text",
    profile="operator",
    goal="Apply the computed target text",
    success_criteria=["The computed target text is saved"],
    input_refs={"target": computed["ref"]},
    data_requirements=[],
    actions=[{
        "name": "enter_target_text",
        "capability": "type",
        "description": "Enter the Runtime-bound target text",
        "input_args": {
            "text": {"input": "target", "path": ["text"]},
        },
    }],
)
if updated["phase"] != "completed":
    ctx.fail(updated["summary"])
ctx.finish(computed["ref"], effect="mutation")
'''.strip()
    )

    assert validate_master_source(source) == []
    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert len(specs) == 1
    assert specs[0].input_refs == {"target": "result:2"}
    binding = specs[0].actions[0].input_args["text"]
    assert binding.input == "target"
    assert binding.path == ["text"]


def test_static_review_rejects_task551_missing_type_text_before_dispatch() -> None:
    source = _program(
        '''
records = ctx.gui_worker(
    worker_id="collect_diana_tights_variants",
    profile="collector",
    goal="Collect matching product variants",
    success_criteria=["All variants are collected"],
    data_requirements=[{
        "id": "variants",
        "description": "Matching product variants",
        "row_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }],
    actions=[{
        "name": "search_products",
        "capability": "type",
        "description": "Search for products by name in the catalog grid",
    }],
)
ctx.fail("fixture stops after review")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "GUI_WORKER_SPEC"
        and "search_products" in item.message
        and "'text'" in item.message
        for item in diagnostics
    )


def test_static_review_rejects_unknown_runtime_bound_action_argument() -> None:
    source = _program(
        '''
result = ctx.transform(
    transform_id="target_url",
    inputs=[],
    source="def transform(inputs):\\n    return {'url': '/target'}",
    result_schema={
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
)
updated = ctx.gui_worker(
    worker_id="open_target",
    profile="operator",
    goal="Open the target",
    success_criteria=["Target is open"],
    input_refs={"target": result["ref"]},
    data_requirements=[],
    actions=[{
        "name": "open_target",
        "capability": "tap",
        "description": "Open the target",
        "input_args": {"target": {"input": "target", "path": ["url"]}},
    }],
)
ctx.finish(result["ref"], effect="ui_state")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "GUI_WORKER_SPEC"
        and "unknown runtime-bound args ['target']" in item.message
        for item in diagnostics
    )


def test_static_review_explains_that_result_ids_cannot_bind_tap_coordinates() -> None:
    source = _program(
        '''
target = ctx.transform(
    transform_id="target_record",
    inputs=[],
    source="def transform(inputs):\\n    return {'id': '42'}",
    result_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
)
opened = ctx.gui_worker(
    worker_id="open_record",
    profile="operator",
    goal="Open the target record",
    success_criteria=["The target record is open"],
    input_refs={"target": target["ref"]},
    data_requirements=[],
    actions=[{
        "name": "open_target",
        "capability": "tap",
        "description": "Open the target record",
        "input_args": {"x": {"input": "target", "path": ["id"]}},
        "exposed_args": ["y"],
    }],
)
ctx.finish(target["ref"], effect="ui_state")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "GUI_WORKER_SPEC"
        and "bind that value to type.text" in item.message
        for item in diagnostics
    )


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
ctx.finish(total["ref"], effect="data")
""".strip()
    )

    assert validate_master_source(source) == []
    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert execution.terminal.phase == "completed"
    assert execution.terminal.effect == "data"
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
ctx.finish(result["ref"], effect="data")
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
result = ctx.transform(
    transform_id="unreachable_count",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return len(inputs[0])",
    result_schema={{"type": "integer"}},
)
ctx.finish(result["ref"], effect="data")
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
ctx.finish(result["ref"], effect="mutation")
""".strip()
    )

    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert store.result_value(execution.terminal.result_ref) is True
