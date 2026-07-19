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
from gui_agent.core.orchestrator.decomposer import _value_contract_issues, decompose
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


def test_decompose_repairs_at_most_once(monkeypatch):
    from gui_agent.core.orchestrator import decomposer as module

    drafts = iter(
        [
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="command",
                        capability="open_url",
                    )
                ]
            ),
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="command",
                        capability="open_url",
                        args={"url": "https://example.test"},
                    )
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
    program = decompose("open the known destination")

    assert len(calls) == 2
    assert isinstance(program.statements[0], Command)
    assert program.statements[0].args["url"] == "https://example.test"
