from gui_agent.core.data_types import AggregateSpec, AggregateStep, FieldRef, ProjectStep
from gui_agent.core.orchestrator import Compute, ComputeRef, OutputSpec, ValueRef
from gui_agent.core.orchestrator.runner import InputDescriptor, StatementInvocation
from gui_agent.core.run.statements.compute import execute_compute_statement


def test_compute_executes_program_defined_semantic_field_pipeline_without_llm():
    statement = Compute(
        id="total",
        bind="answer",
        goal="sum completed order totals",
        source="records",
        required_fields=["order total"],
        inputs={
            "records": ValueRef(var="orders", path=["rows"]),
            "bindings": ValueRef(var="field_map", path=["bindings"]),
        },
        steps=[
            AggregateStep(values={
                "total": AggregateSpec(
                    fn="sum",
                    field=FieldRef(path=["order total"], type="money", semantic=True),
                )
            })
        ],
        outputs={"total": ComputeRef(path=["total"])},
        returns={"total": OutputSpec(type="number")},
    )

    outcome = execute_compute_statement(
        StatementInvocation(
            statement=statement,
            inputs={
                "records": [{"Grand Total": "$12.50"}, {"Grand Total": "$7.25"}],
                "bindings": {"order total": "Grand Total"},
            },
        ),
        observation=None,
    )

    assert outcome.is_completed
    assert outcome.outputs == {"total": 19.75}
    assert outcome.evidence == ["compute:aggregate:2->1"]


def test_compute_reports_missing_semantic_binding_without_retrying_a_plan():
    statement = Compute(
        id="project",
        bind="answer",
        goal="project customer emails",
        source="records",
        required_fields=["customer email"],
        inputs={
            "records": ValueRef(var="orders", path=["rows"]),
            "bindings": ValueRef(var="field_map", path=["bindings"]),
        },
        steps=[ProjectStep(fields={
            "email": FieldRef(path=["customer email"], semantic=True),
        })],
        outputs={"rows": ComputeRef()},
        returns={"rows": OutputSpec(type="list[record]", fields=["email"])},
    )

    outcome = execute_compute_statement(
        StatementInvocation(
            statement=statement,
            inputs={"records": [{"Email": "a@example.test"}], "bindings": {}},
        ),
        observation=None,
    )

    assert outcome.phase == "infeasible"
    assert "semantic field is not bound: customer email" in outcome.summary
    assert outcome.kickback and "Data inspect" in outcome.kickback


def test_compute_preserves_best_effort_input_verification():
    statement = Compute(
        id="count",
        bind="answer",
        goal="count collected rows",
        source="records",
        steps=[AggregateStep(values={"count": AggregateSpec(fn="count")})],
        outputs={"count": ComputeRef(path=["count"])},
        returns={
            "count": OutputSpec(
                type="number",
                coverage="best_effort",
            )
        },
        inputs={"records": ValueRef(var="rows", path=["rows"])},
    )
    outcome = execute_compute_statement(
        StatementInvocation(
            statement=statement,
            inputs={"records": [{"id": 1}]},
            input_descriptors={
                "records": InputDescriptor(
                    source_var="rows",
                    type="list[record]",
                    coverage="best_effort",
                    verification="accepted_unverified",
                )
            },
        ),
        observation=None,
    )

    assert outcome.is_completed
    assert outcome.verification == "accepted_unverified"
