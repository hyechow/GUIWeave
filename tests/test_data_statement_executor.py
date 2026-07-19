import pytest
from pydantic import ValidationError

from gui_agent.core.orchestrator import Data, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.statements import data as module
from gui_agent.core.run.statements.data import (
    DataPlan,
    DataRef,
    EmitOp,
    ReadObservationOp,
    SqlOp,
    execute_data_statement,
)
from gui_agent.core.schemas import Observation


def _invocation(returns):
    return StatementInvocation(
        statement=Data(
            id="derive",
            goal="derive outputs from current runtime data",
            returns=returns,
        )
    )


def test_data_executor_reads_current_observation_and_emits_declared_outputs(monkeypatch):
    plan = DataPlan(
        operations=[
            ReadObservationOp(kind="read_observation", name="location", source="url"),
            EmitOp(kind="emit", values={"url": DataRef(var="location")}),
        ]
    )
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)
    outcome = execute_data_statement(
        _invocation({"url": OutputSpec(type="url")}),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            url="https://example.test/current",
        ),
    )

    assert outcome.is_completed
    assert outcome.outputs == {"url": "https://example.test/current"}
    assert outcome.evidence == ["read_observation:url->location"]


def test_data_executor_can_query_actual_table_snapshot(monkeypatch):
    plan = DataPlan(
        operations=[
            ReadObservationOp(kind="read_observation", name="grid", source="tables"),
            SqlOp(
                kind="sql",
                name="answer",
                source="grid",
                sql="SELECT COUNT(*) AS count FROM data",
                returns=["count"],
            ),
            EmitOp(
                kind="emit",
                values={"count": DataRef(var="answer", path=[0, "count"])},
            ),
        ]
    )
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)
    outcome = execute_data_statement(
        _invocation({"count": OutputSpec(type="number")}),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{"caption": "items", "rows": [{"id": 1}, {"id": 2}]}],
        ),
    )

    assert outcome.outputs == {"count": 2}


def test_data_executor_queries_selected_table_as_row_records(monkeypatch):
    plan = DataPlan(
        operations=[
            ReadObservationOp(name="terms_table", source="tables", path=[1]),
            SqlOp(
                name="ranked",
                source="terms_table",
                sql=(
                    'SELECT "Search Term" AS term, CAST("Uses" AS INTEGER) AS count '
                    'FROM data ORDER BY CAST("Uses" AS INTEGER) DESC LIMIT 2'
                ),
                returns=["term", "count"],
            ),
            EmitOp(values={"terms": DataRef(var="ranked")}),
        ]
    )
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)
    outcome = execute_data_statement(
        _invocation({"terms": OutputSpec(type="list[record]")}),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[
                {"caption": "Other", "rows": [{"value": "ignore"}]},
                {
                    "caption": "Ranked terms",
                    "rows": [
                        {"Search Term": "small", "Uses": "4"},
                        {"Search Term": "large", "Uses": "19"},
                        {"Search Term": "medium", "Uses": "12"},
                    ],
                },
            ],
        ),
    )

    assert outcome.outputs == {
        "terms": [
            {"term": "large", "count": 19},
            {"term": "medium", "count": 12},
        ]
    }


def test_data_executor_projects_structural_table_metadata(monkeypatch):
    plan = DataPlan(
        operations=[
            ReadObservationOp(
                name="total",
                source="tables",
                path=[0, "total_records"],
            ),
            EmitOp(values={"total_count": DataRef(var="total")}),
        ]
    )
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)

    outcome = execute_data_statement(
        _invocation({"total_count": OutputSpec(type="number")}),
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

    assert outcome.is_completed
    assert outcome.outputs == {"total_count": 2}
    assert outcome.evidence == [
        "read_observation:tables[0, 'total_records']->total"
    ]


def test_non_visual_observation_read_rejects_ignored_fields():
    with pytest.raises(ValidationError, match="use path for structural projection"):
        ReadObservationOp(
            name="table_meta",
            source="tables",
            fields={"total_count": "Extract total_records"},
        )


def test_data_executor_has_exactly_one_plan_repair(monkeypatch):
    calls = []

    def bad_plan(*_args, **kwargs):
        calls.append(kwargs.get("previous_error", ""))
        return DataPlan(
            operations=[
                EmitOp(
                    kind="emit",
                    values={"value": DataRef(var="missing")},
                )
            ]
        )

    monkeypatch.setattr(module, "_plan", bad_plan)
    outcome = execute_data_statement(
        _invocation({"value": OutputSpec()}),
        observation=None,
    )

    assert outcome.phase == "exhausted"
    assert len(calls) == 2
    assert calls[0] == ""
    assert "data ref 未定义" in calls[1]
