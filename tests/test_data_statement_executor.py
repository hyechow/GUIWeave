import pytest
from pydantic import ValidationError

from gui_agent.core.orchestrator import Data, OutputSpec, ValueRef
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.statements import data as module
from gui_agent.core.run.statements.data import (
    DataInspection,
    DataRef,
    EmitOp,
    ObservationReadPlan,
    ReadObservationOp,
    execute_data_statement,
)
from gui_agent.core.schemas import Observation


def _read_invocation(returns, *, inputs=None):
    return StatementInvocation(
        statement=Data(
            id="read",
            goal="read declared outputs from the current observation",
            inputs={
                name: ValueRef(var=name)
                for name in (inputs or {})
            },
            returns=returns,
        ),
        inputs=inputs or {},
    )


def _inspect_invocation():
    return StatementInvocation(
        statement=Data(
            id="inspect",
            goal="check whether identity and amount are readable",
            mode="inspect",
            required_fields=["customer identity", "final amount"],
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        )
    )


def test_data_inspect_returns_branchable_field_bindings(monkeypatch):
    monkeypatch.setattr(
        module,
        "_inspect",
        lambda *_args, **_kwargs: DataInspection(
            available=False,
            bindings={"final amount": "Grand Total"},
            missing_fields=["customer identity"],
            reasoning="identity is not present in the current schema",
        ),
    )

    outcome = execute_data_statement(
        _inspect_invocation(),
        observation=Observation(png_bytes=b"png", source="browser"),
    )

    assert outcome.is_completed
    assert outcome.outputs == {
        "available": False,
        "bindings": {"final amount": "Grand Total"},
        "missing_fields": ["customer identity"],
    }


def test_data_inspect_confirms_binding_present_in_input_records(monkeypatch):
    monkeypatch.setattr(
        module,
        "_inspect",
        lambda *_args, **_kwargs: DataInspection(
            available=True,
            bindings={"purchase date": "Purchase Date"},
        ),
    )
    invocation = StatementInvocation(
        statement=Data(
            id="inspect",
            goal="bind purchase date",
            mode="inspect",
            required_fields=["purchase date"],
            inputs={"records": ValueRef(var="orders", path=["rows"])},
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        ),
        inputs={"records": [{"Purchase Date": "Jan 1, 2023"}]},
    )

    outcome = execute_data_statement(invocation, observation=None)

    assert outcome.verification == "confirmed"


def test_observation_read_plan_trace_is_owned_by_data_statement(monkeypatch):
    reports = []

    def invoke(_llm, _messages, schema, **kwargs):
        assert schema is ObservationReadPlan
        assert kwargs["trace_label"] == "statement.data"
        return ObservationReadPlan(
            decision="execute",
            operations=[EmitOp(values={})],
        )

    monkeypatch.setattr(module, "_llm", object)
    monkeypatch.setattr(module, "invoke_structured", invoke)
    module._plan_read(
        _read_invocation({}),
        Observation(png_bytes=b"png", source="browser"),
        context_reports=reports,
    )

    snapshots = [report for report in reports if report.get("kind") == "prompt_snapshot"]
    assert snapshots[0]["label"] == "statement.data"


def test_observation_read_plan_schema_rejects_transform_operations():
    with pytest.raises(ValidationError, match="union_tag_invalid"):
        ObservationReadPlan.model_validate({
            "decision": "execute",
            "operations": [
                {
                    "kind": "transform",
                    "name": "answer",
                    "source": {"var": "records"},
                    "steps": [{"op": "aggregate", "values": {"count": {"fn": "count"}}}],
                },
                {"kind": "emit", "values": {}},
            ],
        })


def test_data_read_rejects_typed_inputs_without_calling_planner(monkeypatch):
    monkeypatch.setattr(
        module,
        "_plan_read",
        lambda *_args, **_kwargs: pytest.fail("planner must not run"),
    )

    outcome = execute_data_statement(
        _read_invocation(
            {"count": OutputSpec(type="number")},
            inputs={"records": [{"id": 1}]},
        ),
        observation=None,
    )

    assert outcome.phase == "exhausted"
    assert "Compute" in outcome.summary


def test_data_reads_current_page_value(monkeypatch):
    plan = ObservationReadPlan(
        decision="execute",
        operations=[
            ReadObservationOp(name="location", source="page", path=["url"]),
            EmitOp(values={"url": DataRef(var="location")}),
        ],
    )
    monkeypatch.setattr(module, "_plan_read", lambda *_args, **_kwargs: plan)

    outcome = execute_data_statement(
        _read_invocation({"url": OutputSpec(type="url")}),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            url="https://example.test/current",
        ),
    )

    assert outcome.is_completed
    assert outcome.outputs == {"url": "https://example.test/current"}
    assert outcome.evidence == ["read_observation:page['url']->location"]


def test_data_reads_normalized_controls_and_semantics(monkeypatch):
    plan = ObservationReadPlan(
        decision="execute",
        operations=[
            ReadObservationOp(name="filter", source="controls", path=[0, "value"]),
            ReadObservationOp(name="heading", source="semantic", path=[0, "key"]),
            EmitOp(values={
                "filter": DataRef(var="filter"),
                "heading": DataRef(var="heading"),
            }),
        ],
    )
    monkeypatch.setattr(module, "_plan_read", lambda *_args, **_kwargs: plan)

    outcome = execute_data_statement(
        _read_invocation({"filter": OutputSpec(), "heading": OutputSpec()}),
        observation=Observation(
            png_bytes=b"png",
            source="android",
            form_controls=[{"label": "Status", "value": "Complete"}],
            semantic_tree=[{"role": "heading", "key": "Order details"}],
        ),
    )

    assert outcome.outputs == {"filter": "Complete", "heading": "Order details"}


def test_data_reads_structural_table_metadata_without_computing(monkeypatch):
    plan = ObservationReadPlan(
        decision="execute",
        operations=[
            ReadObservationOp(name="total", source="datasets", path=[0, "total_records"]),
            EmitOp(values={"total_count": DataRef(var="total")}),
        ],
    )
    monkeypatch.setattr(module, "_plan_read", lambda *_args, **_kwargs: plan)

    outcome = execute_data_statement(
        _read_invocation({"total_count": OutputSpec(type="number")}),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{
                "rows": [{"ID": "199"}, {"ID": "73"}],
                "row_count": 2,
                "total_records": 2,
                "partial": False,
            }],
        ),
    )

    assert outcome.outputs == {"total_count": 2}


def test_data_visual_missing_fact_requests_program_correction(monkeypatch):
    plan = ObservationReadPlan(
        decision="execute",
        operations=[
            ReadObservationOp(
                name="facts",
                source="visual",
                fields={"amount": "the visible final amount"},
            ),
            EmitOp(values={"amount": DataRef(var="facts", path=["amount"])}),
        ],
    )
    monkeypatch.setattr(module, "_plan_read", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.structured_read.structured_read",
        lambda *_args, **_kwargs: {"amount": ""},
    )

    outcome = execute_data_statement(
        _read_invocation({"amount": OutputSpec(type="number")}),
        observation=Observation(png_bytes=b"png", source="iphone"),
    )

    assert outcome.phase == "infeasible"
    assert "Interact" in (outcome.kickback or "")
    assert outcome.outputs == {}


def test_data_unavailable_directs_deterministic_work_to_compute(monkeypatch):
    plan = ObservationReadPlan(
        decision="unavailable",
        reasoning="the requested aggregate is not a directly readable page fact",
        missing_fields=["complete records"],
    )
    monkeypatch.setattr(module, "_plan_read", lambda *_args, **_kwargs: plan)

    outcome = execute_data_statement(
        _read_invocation({"count": OutputSpec(type="number")}),
        observation=Observation(png_bytes=b"png", source="browser"),
    )

    assert outcome.phase == "infeasible"
    assert "Compute" in (outcome.kickback or "")


def test_non_visual_observation_read_rejects_visual_fields():
    with pytest.raises(ValidationError, match="use path for structural projection"):
        ReadObservationOp(
            name="table_meta",
            source="datasets",
            fields={"total_count": "Extract total_records"},
        )


def test_data_reader_has_exactly_one_plan_repair(monkeypatch):
    calls = []

    def bad_plan(*_args, **kwargs):
        calls.append(kwargs.get("previous_error", ""))
        return ObservationReadPlan(
            decision="execute",
            operations=[EmitOp(values={"wrong": DataRef(var="missing")})],
        )

    monkeypatch.setattr(module, "_plan_read", bad_plan)
    outcome = execute_data_statement(
        _read_invocation({"value": OutputSpec()}),
        observation=Observation(png_bytes=b"png", source="browser"),
    )

    assert outcome.phase == "exhausted"
    assert len(calls) == 2
    assert calls[0] == ""
    assert "合法 outputs 仅为 ['value']" in calls[1]
