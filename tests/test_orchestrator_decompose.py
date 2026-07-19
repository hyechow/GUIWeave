from pydantic import ValidationError
import pytest

from gui_agent.core.orchestrator import (
    Acquire,
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


def test_lowering_expands_compact_acquire_contract_from_downstream_data():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data", bind="result", goal="select identities",
            coverage="complete",
            prepare_source="make the required semantic fields readable",
            required_fields=["identity"],
            returns={"rows": OutputSpec(type="list[record]", fields=["identity"])},
        ),
    ]))

    initial, branch, final, acquire, derive = program.statements
    assert isinstance(initial, Data) and initial.mode == "inspect"
    assert isinstance(branch, If) and isinstance(branch.then[0], Interact)
    assert isinstance(final, Data) and final.mode == "inspect"
    assert isinstance(acquire, Acquire)
    assert set(initial.returns) == {"available", "bindings", "missing_fields"}
    assert initial.required_fields == final.required_fields == ["identity"]
    assert acquire.returns["rows"].coverage == "complete"
    assert acquire.returns["rows"].fields == ()
    assert acquire.source_check == ValueRef(var=final.bind, path=["available"])
    assert derive.inputs["records"] == ValueRef(var=acquire.bind, path=["rows"])
    assert validate_program(program) == []


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

    assert [issue.code for issue in issues] == ["DATA_CHAIN_NOT_FUSED"]
    assert issues[0].evidence == ("completed_orders", "ranked_customers")


def test_lowering_does_not_assume_rows_for_data_owned_record_lists():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data",
            bind="ranked",
            goal="derive ranked identities",
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


def test_complete_data_source_is_materialized_but_current_view_is_not():
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
    assert validate_program(complete) == []
    assert validate_program(current) == []


def test_hot_recompile_reuses_declared_collection_binding_without_reacquiring():
    contract = {
        "rows": OutputSpec(type="list[record]", coverage="complete")
    }
    program = to_program(
        _PlanDraft(steps=[_StepDraft(
            op="data",
            bind="answer",
            goal="rank the already collected records",
            coverage="complete",
            required_fields=["customer email"],
            inputs={"records": ValueRef(var="orders", path=["rows"])},
            returns={
                "customers": OutputSpec(type="list[record]", fields=["email"])
            },
        )]),
        initial_collection_binds=frozenset({"orders"}),
    )

    assert len(program.statements) == 1
    assert isinstance(program.statements[0], Data)
    assert program.statements[0].inputs["records"].var == "orders"
    assert validate_program(program, initial_scope={"orders": contract}) == []


def test_lowering_names_anonymous_typed_producer_from_finish_reference():
    program = to_program(_PlanDraft(steps=[
        _StepDraft(
            op="data",
            goal="derive the requested answer",
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
    assert "终态观察" in read.goal
    assert validate_program(program) == []


def test_decompose_repairs_at_most_once(monkeypatch):
    from gui_agent.core.orchestrator import decomposer as module

    drafts = iter(
        [
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="data", bind="counts", goal="group records",
                        returns={"rows": OutputSpec(type="list[record]", fields=["id"])},
                    ),
                    _StepDraft(
                        op="data", bind="answer", goal="rank grouped records",
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
