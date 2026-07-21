from pydantic import ValidationError
import pytest

from gui_agent.core.orchestrator import (
    Acquire,
    Compute,
    ComputeRef,
    Command,
    Data,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    ValueRef,
    validate_program,
)
from gui_agent.core.data_types import (
    AggregateSpec,
    AggregateStep,
    DateBucketStep,
    FieldRef,
    GroupStep,
    ProjectStep,
)
from gui_agent.core.orchestrator._decomposer.draft import (
    _PlanDraft,
    _StepDraft,
    to_program,
)
from gui_agent.core.orchestrator.decomposer import (
    _draft_data_flow_issues,
    _value_contract_issues,
    decompose,
)
from gui_agent.core.router import EntityRef, IntentResolution


def test_draft_converts_only_the_six_semantic_nodes():
    draft = _PlanDraft(
        goal="update selected records",
        steps=[
            _StepDraft(
                op="data",
                bind="selection",
                goal="select records that need a change",
                coverage="current_view",
                returns={"rows": OutputSpec(type="list[record]")},
            ),
            _StepDraft(
                op="foreach",
                items={"var": "selection", "path": ["rows"]},
                item="row",
                body=[
                    _StepDraft(
                        op="interact",
                        goal="apply the requested change to this record",
                        success="the record reflects the requested value",
                        inputs={"row": {"var": "row"}},
                    )
                ],
            ),
            _StepDraft(op="command", capability="back"),
            _StepDraft(
                op="if",
                cond_ref={"var": "selection", "path": ["rows"]},
                cond_cmp="empty",
                then=[_StepDraft(op="finish", message="nothing to do")],
                otherwise=[_StepDraft(op="finish", message="done")],
            ),
        ],
    )

    program = to_program(draft)

    assert isinstance(program.statements[0], Data)
    assert isinstance(program.statements[1], ForEach)
    assert isinstance(program.statements[1].body[0], Interact)
    assert isinstance(program.statements[2], Command)
    assert isinstance(program.statements[3], If)
    assert [
        program.statements[0].id,
        program.statements[1].body[0].id,
        program.statements[2].id,
    ] == ["s1", "s2", "s3"]


def test_complete_data_draft_is_rejected_as_runtime_computation():
    draft = _PlanDraft(steps=[_StepDraft(
        op="data", bind="result", goal="select identities",
        coverage="complete",
        prepare_source="make the required semantic fields readable",
        required_fields=["identity"],
        returns={"rows": OutputSpec(type="list[record]", fields=["identity"])},
    )])
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data", bind="result", goal="select identities",
            coverage="complete",
            prepare_source="make the required semantic fields readable",
            required_fields=["identity"],
            returns={"rows": OutputSpec(type="list[record]", fields=["identity"])},
        ),
    ]))

    assert "DATA_READ_COVERAGE_INVALID" in {
        issue.code for issue in _draft_data_flow_issues(draft)
    }
    assert "DATA_READ_INPUT_FORBIDDEN" in {
        issue.code for issue in validate_program(program)
    }


def test_compiler_rejects_consecutive_data_in_one_linear_block():
    draft = _PlanDraft(steps=[_StepDraft(
        op="if",
        cond_ref=ValueRef(var="orders", path=["available"]),
        then=[
            _StepDraft(
                op="data",
                bind="completed_orders",
                goal="group completed orders by customer",
                coverage="complete",
                required_fields=["customer email", "status"],
                returns={
                    "email_counts": OutputSpec(
                        type="list[record]", fields=["email", "count"],
                    ),
                },
            ),
            _StepDraft(
                op="data",
                bind="ranked_customers",
                goal="rank customers by completed order count",
                coverage="current_view",
                returns={
                    "emails": OutputSpec(type="list[record]", fields=["email"]),
                },
            ),
            _StepDraft(
                op="finish",
                outputs={"result": ValueRef(var="ranked_customers", path=["emails"])},
            ),
        ],
    )])

    issues = _draft_data_flow_issues(draft)

    assert [issue.code for issue in issues] == [
        "DATA_CHAIN_NOT_FUSED",
        "DATA_READ_COVERAGE_INVALID",
    ]
    assert issues[0].evidence == ("completed_orders", "ranked_customers")


def test_lowering_does_not_assume_rows_for_data_owned_record_lists():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data",
            bind="ranked",
            goal="derive ranked identities",
            coverage="current_view",
            returns={
                "emails": OutputSpec(type="list[record]", fields=["email"]),
            },
        ),
        _StepDraft(
            op="if",
            cond_ref=ValueRef(var="ranked", path=["emails"]),
            cond_cmp="exists",
            then=[_StepDraft(
                op="data",
                bind="counted",
                goal="count the ranked identities",
                inputs={"source": ValueRef(var="ranked")},
                returns={"count": OutputSpec(type="number")},
            )],
        ),
    ]))

    branch = program.statements[1]
    assert isinstance(branch, If) and isinstance(branch.then[0], Data)
    assert branch.then[0].inputs["source"] == ValueRef(var="ranked")


def test_lowering_expands_router_owned_exact_then_fallback_lookup():
    resolution = IntentResolution(entities=[EntityRef(
        mention="Diana Tights",
        role="lookup",
        type="product",
        match_mode="approximate",
        search_key="Diana",
    )])
    program = to_program(
        _PlanDraft(steps=[_StepDraft(
            op="lookup",
            goal="make the target product the current result scope",
            lookup_entity="Diana Tights",
            lookup_field="product identity",
            scope="product collection",
            then=[_StepDraft(
                op="interact", goal="open the matching record", success="record is open",
            )],
            otherwise=[_StepDraft(op="finish", message="no matching record")],
        )]),
        resolution=resolution,
    )

    exact, count, fallback, final_count, existence = program.statements
    assert isinstance(exact, Interact)
    assert exact.required_values["query"] == "Diana Tights"
    assert exact.required_values["match_mode"] == "exact"
    assert isinstance(count, Data)
    assert count.returns["match_count"].type == "number"
    assert isinstance(fallback, If)
    assert fallback.cond.value == 0
    assert fallback.then[0].required_values["query"] == "Diana"
    assert fallback.then[0].required_values["lookup_field"] == "product identity"
    assert isinstance(final_count, Data)
    assert isinstance(existence, If) and existence.cond.cmp == ">"
    assert isinstance(existence.then[0], Interact)
    assert isinstance(existence.otherwise[0], Finish)
    assert validate_program(program) == []


def test_router_contract_rejects_phantom_lookup_for_generic_noun():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="lookup",
        lookup_entity="customer",
        lookup_field="customer",
        then=[_StepDraft(op="finish", message="found")],
        otherwise=[_StepDraft(op="finish", message="not found")],
    )]))

    assert {
        issue.code for issue in _value_contract_issues(program, IntentResolution())
    } == {"ROUTER_LOOKUP_NOT_DECLARED"}


def test_complete_data_is_invalid_but_current_view_read_is_not_acquired():
    complete = to_program(_PlanDraft(steps=[_StepDraft(
        op="data",
        bind="answer",
        goal="rank all records by identity frequency",
        coverage="complete",
        required_fields=["identity"],
        returns={"count": OutputSpec(type="number")},
    )]))
    current = to_program(_PlanDraft(steps=[_StepDraft(
        op="data",
        bind="answer",
        goal="count the records visible now",
        coverage="current_view",
        returns={"count": OutputSpec(type="number")},
    )]))

    assert any(isinstance(node, Acquire) for node in complete.statements)
    assert isinstance(complete.statements[-1], Data)
    assert complete.statements[-1].inputs["records"].path == ["rows"]
    assert not any(isinstance(node, Acquire) for node in current.statements)
    assert "DATA_READ_INPUT_FORBIDDEN" in {
        issue.code for issue in validate_program(complete)
    }
    assert validate_program(current) == []


def test_observation_data_defaults_to_current_view_coverage():
    draft = _StepDraft.model_validate({
        "op": "data",
        "bind": "visible_orders",
        "goal": "read visible order fields",
        "required_fields": ["purchase_date", "status"],
        "returns": {
            "rows": {
                "type": "list[record]",
                "fields": ["purchase_date", "status"],
            }
        },
    })

    assert draft.coverage == "current_view"
    program = to_program(_PlanDraft(steps=[draft]))
    assert len(program.statements) == 1
    assert isinstance(program.statements[0], Data)
    assert not any(isinstance(node, Acquire) for node in program.statements)
    assert validate_program(program) == []


def test_data_read_rejects_typed_collection_input_without_reacquiring():
    contract = {
        "rows": OutputSpec(type="list[record]", coverage="complete")
    }
    draft = _StepDraft(
        op="data",
        bind="answer",
        goal="rank the already collected records",
        required_fields=["customer email"],
        inputs={"records": ValueRef(var="orders", path=["rows"])},
        returns={
            "customers": OutputSpec(type="list[record]", fields=["email"])
        },
    )
    program = to_program(
        _PlanDraft(steps=[draft]),
        initial_collection_binds=frozenset({"orders"}),
    )

    assert draft.coverage is None
    assert len(program.statements) == 1
    assert isinstance(program.statements[0], Data)
    assert program.statements[0].inputs["records"].var == "orders"
    assert "DATA_READ_INPUT_FORBIDDEN" in {
        issue.code
        for issue in validate_program(program, initial_scope={"orders": contract})
    }


def test_lowering_names_anonymous_typed_producer_from_finish_reference():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data",
            goal="derive the requested answer",
            coverage="current_view",
            returns={"result": OutputSpec(type="text")},
        ),
        _StepDraft(
            op="finish",
            outputs={"answer": ValueRef(var="data_1", path=["result"])},
        ),
    ]))

    assert program.statements[0].bind == "data_1"
    assert validate_program(program) == []


@pytest.mark.parametrize(
    "payload",
        [
            {"op": "run", "goal": "legacy"},
            {"op": "data"},
            {"op": "command"},
        {"op": "if"},
        {"op": "foreach", "items": {"var": "rows"}, "body": []},
    ],
)
def test_draft_rejects_legacy_or_incomplete_shapes(payload):
    with pytest.raises(ValidationError):
        _StepDraft.model_validate(payload)


def test_lowering_uses_interact_goal_as_default_success_contract():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="interact",
        goal="make the requested view current",
    )]))

    assert program.statements[0].success == "make the requested view current"
    assert validate_program(program) == []


def test_lowering_moves_interact_terminal_read_to_adjacent_data():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="interact",
        bind="read_result",
        goal="make the requested detail view current",
        success="the requested detail view is visible",
        returns={"amount": OutputSpec(type="number", description="visible final amount")},
    )]))

    interact, read = program.statements
    assert isinstance(interact, Interact)
    assert interact.bind is None and interact.returns == {}
    assert isinstance(read, Data)
    assert read.bind == "read_result"
    assert read.returns == {
        "amount": OutputSpec(type="number", description="visible final amount")
    }
    assert "终态 observation" in read.goal
    assert validate_program(program) == []


def test_lowering_inserts_semantic_binding_before_deterministic_compute():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="compute",
        bind="answer",
        goal="sum order totals",
        required_fields=["order total"],
        inputs={"records": ValueRef(var="orders", path=["rows"])},
        compute_source="records",
        compute_steps=[AggregateStep(values={
            "total": AggregateSpec(
                fn="sum",
                field=FieldRef(path=["order total"], type="money", semantic=True),
            )
        })],
        compute_outputs={"total": ComputeRef(path=["total"])},
        returns={"total": OutputSpec(type="number")},
    )]))

    inspect, compute = program.statements
    assert isinstance(inspect, Data) and inspect.mode == "inspect"
    assert isinstance(compute, Compute)
    assert compute.inputs["bindings"].var == inspect.bind
    assert compute.steps[0].values["total"].field.semantic is True
    assert validate_program(
        program,
        initial_scope={"orders": {"rows": OutputSpec(type="list[record]")}},
    ) == []


def test_complete_compute_gets_an_acquired_source_without_runtime_planning():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="compute",
        bind="answer",
        goal="count all orders",
        coverage="complete",
        required_fields=["order identity"],
        compute_source="records",
        compute_steps=[AggregateStep(values={"count": AggregateSpec(fn="count")})],
        compute_outputs={"count": ComputeRef(path=["count"])},
        returns={"count": OutputSpec(type="number")},
    )]))

    compute = program.statements[-1]
    assert isinstance(compute, Compute)
    assert compute.inputs["records"].path == ["rows"]
    assert any(isinstance(statement, Acquire) for statement in program.statements)
    assert validate_program(program) == []


def test_compute_count_needs_no_semantic_field_inspection():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="compute",
        bind="answer",
        goal="count collected orders",
        inputs={"records": ValueRef(var="orders", path=["rows"])},
        compute_source="records",
        compute_steps=[AggregateStep(values={"count": AggregateSpec(fn="count")})],
        compute_outputs={"count": ComputeRef(path=["count"])},
        returns={"count": OutputSpec(type="number")},
    )]))

    assert len(program.statements) == 1
    assert isinstance(program.statements[0], Compute)


def test_compute_unions_declared_and_referenced_semantic_fields_before_acquire():
    program = to_program(_PlanDraft(steps=[_StepDraft(
        op="compute",
        bind="answer",
        goal="count completed orders",
        coverage="complete",
        required_fields=["purchase date"],
        compute_source="records",
        compute_steps=[
            ProjectStep(fields={
                "status": FieldRef(path=["status"], semantic=True),
            })
        ],
        compute_outputs={"rows": ComputeRef()},
        returns={"rows": OutputSpec(type="list[record]", fields=["status"])},
    )]))

    acquired = next(statement for statement in program.statements if isinstance(statement, Acquire))
    inspect = next(
        statement
        for statement in program.statements
        if isinstance(statement, Data) and statement.mode == "inspect"
    )
    compute = next(statement for statement in program.statements if isinstance(statement, Compute))
    assert acquired.required_fields == ["purchase date", "status"]
    assert isinstance(inspect, Data)
    assert inspect.required_fields == ["purchase date", "status"]
    assert isinstance(compute, Compute)
    assert compute.required_fields == ["purchase date", "status"]


def test_incomplete_compute_reaches_compiler_repair_instead_of_schema_failure():
    draft = _PlanDraft(steps=[_StepDraft(
        op="compute",
        bind="answer",
        goal="count orders",
        compute_steps=[AggregateStep(values={"count": AggregateSpec(fn="count")})],
        compute_outputs={"count": ComputeRef(path=["count"])},
        returns={"count": OutputSpec(type="number")},
    )])

    program = to_program(draft)

    assert "COMPUTE_SOURCE_INPUT_REQUIRED" in {
        issue.code for issue in validate_program(program)
    }


def test_draft_normalizes_single_output_return_shorthand():
    draft = _StepDraft.model_validate({
        "op": "compute",
        "goal": "count records",
        "compute_steps": [{"op": "aggregate", "values": {"count": {"fn": "count"}}}],
        "compute_outputs": {"result": {"path": ["count"]}},
        "returns": {
            "type": "number",
            "required": True,
            "description": "record count",
        },
    })

    assert draft.returns == {"result": OutputSpec(type="number", description="record count")}


def test_compute_infers_prior_single_output_source_and_semantic_fields():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data",
            bind="orders_data",
            goal="read filtered orders",
            coverage="current_view",
            returns={"orders": OutputSpec(type="list[record]", fields=["status"])},
        ),
        _StepDraft(
            op="compute",
            bind="answer",
            goal="count completed orders",
            compute_source="orders_data",
            compute_steps=[
                ProjectStep(fields={
                    "status": FieldRef(path=["status"], semantic=True),
                })
            ],
            compute_outputs={"rows": ComputeRef(path=[0])},
            returns={"rows": OutputSpec(type="list[record]", fields=["status"])},
        ),
    ]))

    inspect, compute = program.statements[-2:]
    assert isinstance(inspect, Data)
    assert inspect.required_fields == ["status"]
    assert isinstance(compute, Compute)
    assert compute.source == "records"
    assert compute.inputs["records"] == ValueRef(
        var="orders_data", path=["orders"]
    )
    assert compute.outputs["rows"] == ComputeRef()


def test_complete_compute_lowers_directly_to_acquire():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="compute",
            bind="monthly_counts",
            goal="group orders by month",
            coverage="complete",
            required_fields=["purchase_date"],
            compute_source="records",
            compute_steps=[
                DateBucketStep(
                    field=FieldRef(
                        path=["purchase_date"], type="datetime", semantic=True,
                    ),
                    output="month",
                    format="month_name",
                ),
                GroupStep(
                    by={"month": FieldRef(path=["month"])},
                    values={"count": AggregateSpec(fn="count")},
                ),
            ],
            compute_outputs={"result": ComputeRef()},
            returns={
                "result": OutputSpec(
                    type="list[record]", fields=["month", "count"],
                )
            },
        ),
    ]))

    acquire = next(
        statement for statement in program.statements if isinstance(statement, Acquire)
    )
    compute = program.statements[-1]
    assert acquire.returns["rows"].type == "list[record]"
    assert isinstance(compute, Compute)
    assert compute.inputs["records"] == ValueRef(
        var=acquire.bind, path=["rows"]
    )
    assert validate_program(program) == []


def test_redundant_complete_data_source_is_folded_into_compute_acquire():
    draft = _PlanDraft(steps=[
        _StepDraft(
            op="data",
            goal="read all filtered records",
            coverage="complete",
            required_fields=["purchase date"],
            returns={
                "records": OutputSpec(
                    type="list[record]",
                    fields=["purchase date"],
                ),
            },
        ),
        _StepDraft(
            op="compute",
            bind="monthly_counts",
            goal="group records by month",
            coverage="complete",
            compute_source="records",
            compute_steps=[
                DateBucketStep(
                    field=FieldRef(
                        path=["purchase date"], type="datetime", semantic=True,
                    ),
                    output="month",
                    format="month_name",
                ),
                GroupStep(
                    by={"month": FieldRef(path=["month"])},
                    values={"count": AggregateSpec(fn="count")},
                ),
            ],
            compute_outputs={"result": ComputeRef()},
            returns={
                "result": OutputSpec(
                    type="list[record]", fields=["month", "count"],
                ),
            },
        ),
    ])

    program = to_program(draft)

    acquire = next(statement for statement in program.statements if isinstance(statement, Acquire))
    compute = program.statements[-1]
    assert not any(
        isinstance(statement, Data) and statement.mode == "read"
        for statement in program.statements
    )
    assert isinstance(compute, Compute)
    assert compute.inputs["records"] == ValueRef(var=acquire.bind, path=["rows"])
    assert _draft_data_flow_issues(draft) == []
    assert validate_program(program) == []


def test_decompose_repairs_at_most_once(monkeypatch):
    from gui_agent.core.orchestrator import decomposer as module

    drafts = iter(
        [
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="data", bind="counts", goal="group records",
                        coverage="current_view",
                        returns={"rows": OutputSpec(type="list[record]", fields=["id"])},
                    ),
                    _StepDraft(
                        op="data", bind="answer", goal="rank grouped records",
                        coverage="current_view",
                        returns={"rows": OutputSpec(type="list[record]", fields=["id"])},
                    ),
                    _StepDraft(
                        op="finish",
                        outputs={"result": ValueRef(var="answer", path=["rows"])},
                    ),
                ]
            ),
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="data", bind="answer", goal="group and rank records",
                        coverage="current_view",
                        returns={"rows": OutputSpec(type="list[record]", fields=["id"])},
                    ),
                    _StepDraft(
                        op="finish",
                        outputs={"result": ValueRef(var="answer", path=["rows"])},
                    ),
                ]
            ),
        ]
    )
    calls = []
    monkeypatch.setattr(module, "ChatOpenAI", lambda **_kwargs: object())

    def invoke(*_args, **_kwargs):
        calls.append(1)
        return next(drafts)

    monkeypatch.setattr(module, "invoke_structured", invoke)
    program = decompose("return ranked records")

    assert len(calls) == 2
    assert isinstance(program.statements[0], Data)
    assert program.statements[0].goal == "group and rank records"
