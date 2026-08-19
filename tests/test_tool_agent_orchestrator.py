from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.tool_agent.contracts import WorkerOutcome
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.orchestrator import (
    WorkerOrchestrationContext,
    _schema_contains_array,
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
    approach="Execute the declared actions.",
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


def test_master_review_keeps_method_out_of_immutable_worker_contract() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="collect_weather",
    profile="collector",
    goal="Retrieve weather for 2026-08-18 using the current Bing search page",
    success_criteria=[
        "The search query 'Shenzhen weather' has been executed",
        "Weather for 2026-08-18 is collected",
    ],
    data_requirements=[{
        "id": "weather",
        "description": "Tomorrow's weather",
        "row_schema": {"value": "string"},
    }],
    approach="Type the query and press Enter in Bing search",
)
ctx.fail(outcome["summary"])
'''.strip()
    )

    diagnostics = validate_master_source(
        source,
        user_goal="明天的天气",
    )

    assert {item.code for item in diagnostics} >= {
        "DATA_FILTER_BOUNDARY",
        "WORKER_GOAL_BOUNDARY",
        "WORKER_SUCCESS_BOUNDARY",
        "WORKER_APPROACH_BOUNDARY",
    }
    filter_issue = next(
        item for item in diagnostics if item.code == "DATA_FILTER_BOUNDARY"
    )
    assert "2026-08-18" in filter_issue.message
    assert "Shenzhen weather" not in filter_issue.message


def test_master_review_does_not_parse_possessive_as_quoted_scope() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="collect_records",
    profile="collector",
    goal="Compare each candidate's primary record against the phrase 'requested class'.",
    success_criteria="Matching records are collected.",
    approach="authoritative record index",
    data_requirements=[{
        "id": "records",
        "description": "Records matching the requested class",
        "cardinality": "many",
        "row_schema": {"record": "string"},
        "field_sources": {"record": "Record"},
        "field_types": {"record": "text"},
        "filters": {"record_contains": "requested class"},
        "coverage": "complete",
    }],
)
ctx.fail(outcome["summary"])
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert not [item for item in diagnostics if item.code == "DATA_FILTER_BOUNDARY"]


def test_master_review_accepts_semantic_contract_and_source_approach() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="collect_weather",
    profile="collector",
    goal="Retrieve tomorrow's weather",
    success_criteria=["Tomorrow's weather is collected"],
    data_requirements=[{
        "id": "weather",
        "description": "Tomorrow's weather",
        "row_schema": {"value": "string"},
    }],
    approach="Use Bing weather results as the initial source",
)
ctx.fail(outcome["summary"])
'''.strip()
    )

    diagnostics = validate_master_source(
        source,
        user_goal="明天的天气",
    )

    assert not any(item.code.startswith("WORKER_") for item in diagnostics)


def test_master_review_rejects_collector_surface_success() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="collect_weather",
    profile="collector",
    goal="Retrieve tomorrow's weather",
    success_criteria=["The search results page is visible"],
    approach="Public weather index",
    data_requirements=[{
        "id": "weather",
        "description": "Tomorrow's weather",
        "cardinality": "one",
        "row_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "temperature": {"type": "string"},
                "condition": {"type": "string"},
            },
            "required": ["date", "temperature", "condition"],
        },
        "field_sources": {"date": "Date", "temperature": "Temperature", "condition": "Condition"},
        "field_types": {"date": "text", "temperature": "text", "condition": "text"},
        "filters": {"date": "2026-08-18"},
        "coverage": "first_match",
    }],
)
ctx.fail(outcome["summary"])
'''.strip()
    )

    codes = {
        item.code for item in validate_master_source(source, user_goal="明天深圳的天气")
    }

    assert "WORKER_SUCCESS_BOUNDARY" in codes


def test_master_review_rejects_current_source_method_in_goal() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="collect_weather",
    profile="collector",
    goal="Retrieve the forecast from the current search results page",
    success_criteria=["The requested forecast is collected"],
    data_requirements=[{
        "id": "weather",
        "description": "Requested forecast",
        "row_schema": {"value": "string"},
    }],
    approach="Use the current search provider as the initial source",
)
ctx.fail(outcome["summary"])
'''.strip()
    )

    diagnostics = validate_master_source(source, user_goal="明天的天气")

    assert any(item.code == "WORKER_GOAL_BOUNDARY" for item in diagnostics)


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
    literal = validate_master_source(
        "def run(ctx):\n    ctx.finish(None, effect='data')"
    )

    assert any(item.code == "REF_VALUE_REQUIRED" for item in descriptor_access)
    assert any(item.code == "ATTRIBUTE_ACCESS" for item in attribute_access)
    assert any(item.code == "REF_VALUE_REQUIRED" for item in literal)


def test_master_review_rejects_collection_ref_from_operator() -> None:
    source = _program(
        '''
outcome = ctx.gui_worker(
    worker_id="read_records",
    profile="operator",
    goal="Read the requested records",
    success_criteria=["Every requested record was read"],
    data_requirements=[],
    approach="Execute the declared actions.",
)
result = ctx.transform(
    transform_id="count_records",
    inputs=[outcome["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return len(inputs[0])",
    result_schema={"type": "integer"},
)
ctx.finish(result["ref"], effect="data")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "OPERATOR_COLLECTION_REF" for item in diagnostics)


def test_master_review_explains_visual_only_worker_dependencies() -> None:
    source = _program(
        '''
result = ctx.worker_result("collect")
target_ref = result["collection_ref"]["ref"]
outcome = ctx.gui_worker(
    worker_id="continue_visual_branch",
    profile="operator",
    goal="Continue the visual branch",
    success_criteria=["The visual branch is complete"],
    input_refs={"target": target_ref},
    data_requirements=[],
    approach="Execute the declared actions.",
)
if outcome["phase"] != "completed":
    ctx.fail(outcome["summary"])
ctx.fail("invalid program")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "REF_VALUE_REQUIRED"
        and "must stay inside one cohesive operator" in item.message
        for item in diagnostics
    )
    assert any(
        item.code == "GUI_WORKER_INPUT_BINDING"
        and "merge visual-only or conditional dependencies" in item.message
        for item in diagnostics
    )


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
    approach="Execute the declared actions.",
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
    approach="Execute the declared actions.",
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
    approach="Execute the declared actions.",
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
    approach="Execute the declared actions.",
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
    approach="Execute the declared actions.",
)
if located["phase"] != "completed":
    ctx.fail(located["summary"])
updated = ctx.gui_worker(
    worker_id="update_record",
    profile="operator",
    goal="Update the currently open record",
    success_criteria=["The record is saved"],
    data_requirements=[],
    approach="Execute the declared actions.",
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
    approach="Execute the declared actions.",
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


def test_master_review_finds_array_nested_in_private_schema() -> None:
    assert _schema_contains_array(
        {"type": "object", "properties": {"names": {"type": "array"}}}
    )


def test_master_review_accepts_explicit_task544_contract() -> None:
    source = _program(
        '''
reviews = ctx.gui_worker(
    worker_id="collect_reviews",
    profile="collector",
    goal="Collect reviews for one product",
    success_criteria=["All matching review ratings are collected."],
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
        "coverage": "complete",
    }],
    approach="Execute the declared actions.",
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
    success_criteria=["The product is saved successfully."],
    input_refs={"computed": computed["ref"]},
    data_requirements=[],
    approach="Execute the declared actions.",
    input_bindings=[{
        "name": "enter_text",
        "input": "computed",
        "path": ["text"],
        "target": "text_input",
        "description": "Enter the computed text",
    }],
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
    approach="Execute the declared actions.",
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


def test_master_review_accepts_static_scalar_aliases_in_worker_contract() -> None:
    source = _program(
        '''
target_date = "2026-08-18"
outcome = ctx.gui_worker(
    worker_id="collect_forecast",
    profile="collector",
    goal=f"Collect the forecast for {target_date}",
    success_criteria=f"The forecast for {target_date} is collected",
    data_requirements=[{
        "id": "forecast",
        "description": f"Forecast for {target_date}",
        "row_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
            "additionalProperties": False,
        },
        "field_sources": {"date": "Date"},
        "filters": {"date": target_date},
        "coverage": "first_match",
    }],
    approach="public forecast source",
)
if outcome["phase"] != "completed":
    ctx.fail(outcome["summary"])
result = ctx.transform(
    transform_id="forecast_result",
    inputs=[outcome["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return inputs[0]",
    result_schema={"type": "array"},
)
ctx.finish(result["ref"], effect="data")
'''.strip()
    )

    assert validate_master_source(source) == []
    captured = []
    ctx = WorkerOrchestrationContext(
        data_store=RuntimeDataStore(),
        run_gui_worker=lambda _worker_id, spec: (
            captured.append(spec.success_criteria)
            or WorkerOutcome(phase="failed", summary="stop after validation", steps=0)
        ),
        trace=lambda *args, **kwargs: None,
    )

    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert captured == [["The forecast for 2026-08-18 is collected"]]


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
    approach="Execute the declared actions.",
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
    input_bindings=[{
        "name": "enter_target_text",
        "input": "target",
        "path": ["text"],
        "target": "text_input",
        "description": "Enter the computed target text",
    }],
    data_requirements=[],
    approach="Execute the declared actions.",
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
    binding = specs[0].input_bindings[0]
    assert binding.input == "target"
    assert binding.path == ["text"]


def test_static_review_does_not_require_master_to_declare_type_text() -> None:
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
    approach="Execute the declared actions.",
)
ctx.fail("fixture stops after review")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert not any(item.code == "GUI_WORKER_SPEC" for item in diagnostics)


def test_static_review_rejects_unknown_input_binding_target() -> None:
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
    input_bindings=[{
        "name": "open_bound_target",
        "input": "target",
        "path": ["url"],
        "target": "unsupported_target",
        "description": "Open the computed target URL",
    }],
    data_requirements=[],
    approach="Execute the declared actions.",
)
ctx.finish(result["ref"], effect="ui_state")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "GUI_WORKER_SPEC"
        and "target" in item.message
        for item in diagnostics
    )


def test_static_review_rejects_spatial_input_binding_target() -> None:
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
    input_bindings=[{
        "name": "tap_bound_target",
        "input": "target",
        "path": ["id"],
        "target": "tap_point",
        "description": "Tap the computed target",
    }],
    data_requirements=[],
    approach="Launch {app} and confirm that its requested surface is visible.",
)
ctx.finish(target["ref"], effect="ui_state")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(
        item.code == "GUI_WORKER_SPEC"
        and "target" in item.message
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


def test_master_review_rejects_data_collection_for_destination_only_goal() -> None:
    source = _program(
        f'''\
records = {GUI_CALL}
result = ctx.transform(
    transform_id="return_records",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return inputs[0]",
    result_schema={{"type": "array", "items": {ROW_SCHEMA!r}}},
)
ctx.finish(result["ref"], effect="data")
'''.strip()
    )

    diagnostics = validate_master_source(source, user_goal="查看订单列表")

    assert any(item.code == "TASK_INTENT" for item in diagnostics)
    inferred = source.replace('profile="collector",\n', "")
    assert any(
        item.code == "TASK_INTENT"
        for item in validate_master_source(inferred, user_goal="打开设置")
    )
    assert not any(
        item.code == "TASK_INTENT"
        for item in validate_master_source(source, user_goal="查看有多少订单的订单列表")
    )


def test_master_review_accepts_operator_for_destination_only_goal() -> None:
    assert validate_master_source(
        _launch_app_program("settings"), user_goal="查看订单列表"
    ) == []


def _launch_app_program(app: str) -> str:
    return _program(
        f'''\
ctx.gui_worker(
    worker_id="open_settings",
    profile="operator",
    goal="Open the requested application",
    success_criteria=["The requested application is visible"],
    data_requirements=[],
    approach="Execute the declared actions.",
)
done = ctx.transform(
    transform_id="confirm_application_open",
    inputs=[],
    source="def transform(inputs):\\n    return True",
    result_schema={{"type": "boolean"}},
)
ctx.finish(done["ref"], effect="ui_state")
'''.strip()
    )


def test_master_review_leaves_launch_argument_validation_to_runtime() -> None:
    assert validate_master_source(
        _launch_app_program("Settings"),
        platform_context={
            "name": "android",
            "applications": ["com.android.settings/.HWSettings"],
        },
    ) == []
    assert validate_master_source(
        _launch_app_program("com.android.settings/.HWSettings"),
        platform_context={
            "name": "android",
            "applications": ["com.android.settings/.HWSettings"],
        },
    ) == []


def test_master_compiler_does_not_regenerate_for_runtime_action_choice() -> None:
    source = _launch_app_program("Settings")
    llm = _SequenceLLM(source)
    events = []

    program = compile_master_program(
        llm=llm,
        system_prompt="Compile a program.",
        task_context={
            "goal": "Open settings",
            "platform": {
                "name": "android",
                "applications": ["com.android.settings/.HWSettings"],
            },
        },
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert program.source == source
    assert program.attempts == 1
    assert events[0][1]["diagnostics"] == []


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


def test_coding_master_finishes_plain_collection_without_transform() -> None:
    store = RuntimeDataStore()
    rows = [{"amount": 2}, {"amount": 3}]

    def run_gui_worker(worker_id, spec):
        assert worker_id == "collect_records"
        assert spec.profile == "collector"
        return WorkerOutcome(
            phase="completed",
            summary="Collected records",
            collection_ref=_put_complete_collection(store, rows),
            steps=1,
        )

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=run_gui_worker,
        trace=lambda *args, **kwargs: None,
    )
    source = _program(
        f'''\
records = {GUI_CALL}
if records["phase"] != "completed":
    ctx.fail(records["summary"])
ctx.finish(records["collection_ref"]["ref"], effect="data")
'''.strip()
    )

    assert validate_master_source(source) == []
    execution = execute_master_program(source, ctx)

    assert execution.ok
    assert execution.terminal is not None
    assert execution.terminal.effect == "data"
    assert store.result_value(execution.terminal.result_ref) == rows
    assert store.result_descriptor(execution.terminal.result_ref).value_schema == {
        "type": "array",
        "items": ROW_SCHEMA,
    }
    assert store.is_data_result(execution.terminal.result_ref)


def test_collection_ref_can_only_finish_as_data() -> None:
    source = _program(
        f'''\
records = {GUI_CALL}
ctx.finish(records["collection_ref"]["ref"], effect="mutation")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "COLLECTION_FINISH_EFFECT" for item in diagnostics)


def test_invalid_collector_finished_as_data_reports_only_its_contract_error() -> None:
    source = _program(
        '''
records = ctx.gui_worker(
    worker_id="collect_records",
    profile="collector",
    goal="Collect the requested records",
    success_criteria="The requested records are collected",
    approach="current data source",
    data_requirements=[{
        "id": "records",
        "description": "Requested records",
        "row_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        "filters": {"scope": "requested"},
    }],
)
ctx.finish(records["collection_ref"]["ref"], effect="data")
'''.strip()
    )

    diagnostics = validate_master_source(source)

    assert any(item.code == "GUI_WORKER_SPEC" for item in diagnostics)
    assert not any(item.code == "COLLECTOR_RESULT_UNUSED" for item in diagnostics)


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


def test_reexecuted_frozen_program_reuses_failed_gui_worker() -> None:
    store = RuntimeDataStore()
    gui_calls = []
    trace = []

    def fail_gui_worker(worker_id, spec):
        assert worker_id == "collect_records"
        gui_calls.append(worker_id)
        del spec
        return WorkerOutcome(phase="failed", summary="Unexpected access gate", steps=1)

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=fail_gui_worker,
        trace=lambda event, **payload: trace.append({"event": event, **payload}),
    )
    source = _program(
        f"""
records = {GUI_CALL}
if records["phase"] != "completed":
    ctx.fail(records["summary"])
result = ctx.transform(
    transform_id="unreachable_count",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return len(inputs[0])",
    result_schema={{"type": "integer"}},
)
ctx.finish(result["ref"], effect="data")
""".strip()
    )

    first = execute_master_program(source, ctx)
    second = execute_master_program(source, ctx)

    assert first.ok and second.ok
    assert first.terminal is not None and second.terminal is not None
    assert first.terminal.phase == second.terminal.phase == "failed"
    assert first.terminal.summary == second.terminal.summary == "Unexpected access gate"
    assert gui_calls == ["collect_records"]
    assert any(item["event"] == "master_worker_reuse" for item in trace)


def test_master_failure_preserves_latest_worker_blocker() -> None:
    store = RuntimeDataStore()

    def fail_gui_worker(_worker_id, _spec):
        return WorkerOutcome(
            phase="failed",
            summary="The current public source denied access.",
            steps=1,
        )

    ctx = WorkerOrchestrationContext(
        data_store=store,
        run_gui_worker=fail_gui_worker,
        trace=lambda *args, **kwargs: None,
    )
    source = _program(
        '''\
outcome = ctx.gui_worker(
    worker_id="open_page", profile="operator", goal="Open the requested page",
    success_criteria=["The page is visible"],
    approach="Execute the declared actions.",
)
if outcome["phase"] != "completed":
    ctx.fail("Could not retrieve the requested result")
ctx.fail("unreachable")
'''.strip()
    )

    execution = execute_master_program(source, ctx)

    assert execution.terminal is not None
    assert execution.terminal.summary == (
        "Could not retrieve the requested result — "
        "The current public source denied access."
    )


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
    approach="Execute the declared actions.",
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

    rejected = execute_master_program(source.replace('effect="mutation"', 'effect="data"'), ctx)
    assert "derived from collected data" in rejected.error


def test_data_program_allows_guarded_operator_before_dependent_collector() -> None:
    source = _program(f'''\
opened = ctx.gui_worker(
    worker_id="open_records",
    profile="operator",
    goal="Open the records surface",
    success_criteria=["The records surface is visible"],
    data_requirements=[],
    approach="Execute the declared actions.",
)
if opened["phase"] != "completed":
    ctx.fail(opened["summary"])
records = {GUI_CALL}
result = ctx.transform(
    transform_id="sum_records",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return sum(row['amount'] for row in inputs[0])",
    result_schema={{"type": "number"}},
)
ctx.finish(result["ref"], effect="data")
'''.strip())

    diagnostics = validate_master_source(source)

    assert not [
        item for item in diagnostics
        if item.code.startswith("DATA_OPERATOR")
    ]


def test_master_review_does_not_interpret_operator_control_flow() -> None:
    source = _program(f'''\
opened = ctx.gui_worker(
    worker_id="open_records",
    profile="operator",
    goal="Open the records surface",
    success_criteria=["The records surface is visible"],
    data_requirements=[],
    approach="Execute the declared actions.",
)
records = {GUI_CALL}
result = ctx.transform(
    transform_id="sum_records",
    inputs=[records["collection_ref"]["ref"]],
    source="def transform(inputs):\\n    return sum(row['amount'] for row in inputs[0])",
    result_schema={{"type": "number"}},
)
ctx.finish(result["ref"], effect="data")
'''.strip())

    assert validate_master_source(source) == []
