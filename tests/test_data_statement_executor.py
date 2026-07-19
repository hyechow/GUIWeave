import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gui_agent.core.orchestrator import Data, OutputSpec
from gui_agent.core.orchestrator.runner import InputDescriptor, StatementInvocation
from gui_agent.core.run.statements import data as module
from gui_agent.core.run.statements.data import (
    DataInspection,
    DataPlan,
    DataRef,
    EmitOp,
    ReadObservationOp,
    TransformOp,
    execute_data_statement,
)
from gui_agent.core.run.statements.data_kernel import (
    AggregateSpec,
    AggregateStep,
    DataKernelError,
    FieldRef,
    GroupStep,
    ProjectStep,
    RankStep,
    SortKey,
    SortStep,
    TakeStep,
)
from gui_agent.core.schemas import Observation


def _invocation(returns, *, inputs=None):
    return StatementInvocation(
        statement=Data(
            id="derive",
            goal="derive outputs from current runtime data",
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


def test_data_inspect_returns_branchable_availability_without_ui_actions(monkeypatch):
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


def test_data_plan_trace_is_owned_by_statement_runtime(monkeypatch):
    reports = []

    def invoke(_llm, _messages, _schema, **kwargs):
        assert kwargs["trace_label"] == "statement.data"
        return DataPlan(decision="execute", operations=[EmitOp(values={})])

    monkeypatch.setattr(module, "_llm", object)
    monkeypatch.setattr(module, "invoke_structured", invoke)
    module._plan(
        _invocation({}),
        Observation(png_bytes=b"png", source="browser"),
        context_reports=reports,
    )

    snapshots = [report for report in reports if report.get("kind") == "prompt_snapshot"]
    assert snapshots[0]["label"] == "statement.data"


def test_materialized_collection_is_the_only_dataset_in_data_context():
    rows = [
        {"Email": "first@example.test", "Status": "Complete"},
        {"Email": "second@example.test", "Status": "Complete"},
        {"Email": "third@example.test", "Status": "Complete"},
        {"Email": "must-not-enter-prompt@example.test", "Status": "Complete"},
    ]
    invocation = StatementInvocation(
        statement=Data(id="derive", goal="rank completed customers"),
        inputs={"records": rows},
        input_descriptors={
            "records": InputDescriptor(
                source_var="orders",
                producer="acquire",
                output_name="rows",
                type="list[record]",
                coverage="complete",
                verification="confirmed",
            )
        },
    )
    context = module._context_summary(
        invocation,
        Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{
                "rows": [{"Email": "current-page-only@example.test"}],
                "partial": True,
                "total_records": 308,
            }],
        ),
    )

    assert '"source_authority": "materialized_inputs"' in context
    assert '"authoritative": true' in context
    assert "must-not-enter-prompt@example.test" not in context
    assert "current-page-only@example.test" not in context


def test_confirmed_complete_input_repairs_false_partial_unavailable(monkeypatch):
    plans = iter([
        DataPlan(
            decision="unavailable",
            reasoning="the current page is only a partial window",
            required_coverage="complete",
        ),
        DataPlan(
            decision="execute",
            operations=[
                TransformOp(
                    name="ranked",
                    source=DataRef(var="records"),
                    steps=[
                        GroupStep(
                            by={"email": FieldRef(path=["Customer Email"])},
                            values={"count": AggregateSpec(fn="count")},
                        ),
                        RankStep(
                            keys=[SortKey(
                                field=FieldRef(path=["count"], type="number"),
                                direction="desc",
                            )],
                            position=2,
                        ),
                        ProjectStep(fields={
                            "email": FieldRef(path=["email"], type="text"),
                        }),
                    ],
                ),
                EmitOp(values={"customers": DataRef(var="ranked")}),
            ],
        ),
    ])
    errors = []

    def plan(*_args, **kwargs):
        errors.append(kwargs.get("previous_error", ""))
        return next(plans)

    monkeypatch.setattr(module, "_plan", plan)
    invocation = StatementInvocation(
        statement=Data(
            id="rank",
            goal="get customer emails with the second most completed orders",
            inputs={},
            returns={
                "customers": OutputSpec(
                    type="list[record]",
                    fields=["email"],
                )
            },
        ),
        inputs={
            "records": [
                {"Customer Email": "a@example.test"},
                {"Customer Email": "a@example.test"},
                {"Customer Email": "a@example.test"},
                {"Customer Email": "b@example.test"},
                {"Customer Email": "b@example.test"},
                {"Customer Email": "c@example.test"},
            ]
        },
        input_descriptors={
            "records": InputDescriptor(
                source_var="orders",
                producer="acquire",
                output_name="rows",
                type="list[record]",
                coverage="complete",
                verification="confirmed",
            )
        },
    )

    outcome = execute_data_statement(invocation, observation=None)

    assert outcome.is_completed
    assert outcome.outputs == {"customers": [{"email": "b@example.test"}]}
    assert errors[0] == ""
    assert "confirmed complete materialized input" in errors[1]


def test_recorded_rank_emit_path_replay_gets_typed_repair(monkeypatch):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/data_replay/205135_rank_emit_path.json")
        .read_text(encoding="utf-8")
    )
    bad = DataPlan.model_validate(fixture["bad_plan"])
    repaired = DataPlan.model_validate(fixture["repaired_plan"])
    invocation = _invocation(
        {"emails": OutputSpec(type="list[record]", fields=["email"])},
        inputs={"records": fixture["records"]},
    )
    errors = []

    with pytest.raises(DataKernelError, match=r"list\[record\].*path=\['email'\]"):
        module._preflight_data_plan(bad, invocation, None)

    plans = iter([bad, repaired])

    def plan(*_args, **kwargs):
        errors.append(kwargs.get("previous_error", ""))
        return next(plans)

    monkeypatch.setattr(module, "_plan", plan)
    outcome = execute_data_statement(invocation, observation=None)

    assert outcome.is_completed
    assert outcome.outputs == fixture["expected"]
    assert errors[0] == ""
    assert "输出记录列表请使用 path=[]" in errors[1]
    assert "transform.project" in errors[1]


def test_data_executor_reads_current_observation_and_emits_declared_outputs(monkeypatch):
    plan = DataPlan(
        decision="execute",
        operations=[
            ReadObservationOp(
                kind="read_observation", name="location", source="page", path=["url"]
            ),
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
    assert outcome.evidence == ["read_observation:page['url']->location"]


def test_data_executor_reads_normalized_controls_and_semantics(monkeypatch):
    plan = DataPlan(
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
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)
    outcome = execute_data_statement(
        _invocation({"filter": OutputSpec(), "heading": OutputSpec()}),
        observation=Observation(
            png_bytes=b"png",
            source="android",
            form_controls=[{"label": "Status", "value": "Complete"}],
            semantic_tree=[{"role": "heading", "key": "Order details"}],
        ),
    )

    assert outcome.outputs == {"filter": "Complete", "heading": "Order details"}


def test_data_visual_missing_fact_requests_program_correction_without_false_value(monkeypatch):
    plan = DataPlan(
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
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.structured_read.structured_read",
        lambda *_args, **_kwargs: {"amount": ""},
    )
    outcome = execute_data_statement(
        _invocation({"amount": OutputSpec(type="number")}),
        observation=Observation(png_bytes=b"png", source="iphone"),
    )

    assert outcome.phase == "infeasible"
    assert "Interact" in (outcome.kickback or "")
    assert outcome.outputs == {}


def test_data_executor_can_transform_actual_table_snapshot(monkeypatch):
    plan = DataPlan(
        decision="execute",
        operations=[
            ReadObservationOp(
                kind="read_observation",
                name="grid",
                source="datasets",
                path=[0],
            ),
            TransformOp(
                kind="transform",
                name="answer",
                source=DataRef(var="grid"),
                steps=[
                    AggregateStep(values={"count": AggregateSpec(fn="count")}),
                ],
            ),
            EmitOp(
                kind="emit",
                values={"count": DataRef(var="answer", path=["count"])},
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


def test_data_executor_transforms_selected_table_as_row_records(monkeypatch):
    plan = DataPlan(
        decision="execute",
        operations=[
            ReadObservationOp(name="terms_table", source="datasets", path=[1]),
            TransformOp(
                name="ranked",
                source=DataRef(var="terms_table"),
                steps=[
                    SortStep(keys=[SortKey(
                        field=FieldRef(path=["Uses"], type="number"),
                        direction="desc",
                    )]),
                    TakeStep(count=2),
                    ProjectStep(fields={
                        "term": FieldRef(path=["Search Term"], type="text"),
                        "count": FieldRef(path=["Uses"], type="number"),
                    }),
                ],
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
        decision="execute",
        operations=[
            ReadObservationOp(
                name="total",
                source="datasets",
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
        "read_observation:datasets[0, 'total_records']->total"
    ]


@pytest.mark.parametrize(
    ("coverage", "phase", "verification"),
    [
        ("current_view", "completed", "confirmed"),
        ("best_effort", "completed", "accepted_unverified"),
        ("complete", "exhausted", None),
    ],
)
def test_data_executor_enforces_partial_source_coverage(
    monkeypatch,
    coverage,
    phase,
    verification,
):
    plan = DataPlan(decision="execute", operations=[
        ReadObservationOp(name="grid", source="datasets", path=[0]),
        TransformOp(
            name="rows",
            source=DataRef(var="grid"),
            steps=[ProjectStep(fields={"id": FieldRef(path=["ID"])})],
        ),
        EmitOp(values={"rows": DataRef(var="rows")}),
    ])
    monkeypatch.setattr(module, "_plan", lambda *_args, **_kwargs: plan)

    outcome = execute_data_statement(
        _invocation({
            "rows": OutputSpec(type="list[record]", coverage=coverage),
        }),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{"rows": [{"ID": "1"}], "partial": True}],
        ),
    )

    assert outcome.phase == phase
    assert outcome.verification == verification


def test_non_visual_observation_read_rejects_ignored_fields():
    with pytest.raises(ValidationError, match="use path for structural projection"):
        ReadObservationOp(
            name="table_meta",
            source="datasets",
            fields={"total_count": "Extract total_records"},
        )


def test_data_executor_has_exactly_one_plan_repair(monkeypatch):
    calls = []

    def bad_plan(*_args, **kwargs):
        calls.append(kwargs.get("previous_error", ""))
        return DataPlan(
            decision="execute",
            operations=[
                EmitOp(
                    kind="emit",
                    values={"wrong": DataRef(var="missing")},
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
    assert "合法 outputs 仅为 ['value']" in calls[1]


def test_data_executor_repairs_wrong_output_shape_into_data_requirement(monkeypatch):
    plans = iter([
        DataPlan(
            decision="execute",
            operations=[
                ReadObservationOp(name="orders", source="datasets", path=[0]),
                EmitOp(values={"emails": DataRef(var="orders")}),
            ],
        ),
        DataPlan(
            decision="unavailable",
            reasoning="Orders 数据只有 20/153 行且没有客户邮箱字段",
            missing_fields=["customer email"],
            required_coverage="complete",
        ),
    ])
    errors = []
    messages = []
    statuses = []

    def plan(*_args, **kwargs):
        errors.append(kwargs.get("previous_error", ""))
        return next(plans)

    monkeypatch.setattr(module, "_plan", plan)
    outcome = execute_data_statement(
        _invocation({"emails": OutputSpec(type="list[record]")}),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{
                "rows": [{"ID": "1", "Status": "Complete"}],
                "total_records": 153,
                "partial": True,
            }],
        ),
        say=messages.append,
        status=statuses.append,
    )

    assert outcome.phase == "infeasible"
    assert "customer email" in (outcome.kickback or "")
    assert "Data.inputs" in (outcome.kickback or "")
    assert len(errors) == 2
    assert "类型不符合合同" in errors[1]
    assert any("计划 1/2 失败" in message for message in messages)
    assert statuses[-1] == "Data 数据不足，正在请求重编排…"


def test_data_executor_rejects_records_missing_declared_fields(monkeypatch):
    plans = iter([
        DataPlan(
            decision="execute",
            operations=[
                TransformOp(
                    name="ranked",
                    source=DataRef(var="collection"),
                    steps=[
                        GroupStep(
                            by={"display_name": FieldRef(path=["Display Name"])},
                            values={"count": AggregateSpec(fn="count")},
                        ),
                        RankStep(
                            keys=[SortKey(
                                field=FieldRef(path=["count"], type="number"),
                                direction="desc",
                            )],
                            position=2,
                        ),
                    ],
                ),
                EmitOp(values={"contacts": DataRef(var="ranked")}),
            ],
        ),
        DataPlan(
            decision="unavailable",
            reasoning="The acquired records do not contain the required contact address.",
            missing_fields=["contact address"],
            required_coverage="complete",
        ),
    ])
    errors = []

    def plan(*_args, **kwargs):
        errors.append(kwargs.get("previous_error", ""))
        return next(plans)

    monkeypatch.setattr(module, "_plan", plan)
    outcome = execute_data_statement(
        _invocation(
            {"contacts": OutputSpec(type="list[record]", fields=["address"])},
            inputs={
                "collection": [
                    {"Display Name": "A", "State": "Done"},
                    {"Display Name": "A", "State": "Done"},
                    {"Display Name": "B", "State": "Done"},
                ],
            },
        ),
        observation=None,
    )

    assert outcome.phase == "infeasible"
    assert "contact address" in (outcome.kickback or "")
    assert "类型不符合合同" in errors[1]
