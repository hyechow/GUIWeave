"""Minimal Statement kernel: one Transition call plus mechanical validation."""

from __future__ import annotations

import io

from PIL import Image
import pytest

from llm.structured import StructuredOutputError
from gui_agent.core.filter_contract import (
    AppliedFilterState,
    compile_filter_predicates,
)
from gui_agent.core.schemas import (
    ActionSignal,
    ActionIntent,
    CollectionIntent,
    Observation,
    PolicyTurn,
    StatementContract,
    StatementOutcome,
    StatementOutcomeEvent,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


INSTANCE = "run:s1"


@pytest.fixture(autouse=True)
def _frame_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _observation: False)


def _png() -> bytes:
    stream = io.BytesIO()
    image = Image.new("RGB", (64, 64), "white")
    for x in range(32):
        for y in range(64):
            image.putpixel((x, y), (0, 0, 0))
    image.save(stream, format="PNG")
    return stream.getvalue()


def _assessment(status: str, *, gap: str = "target is not reached") -> _TransitionAssessment:
    return _TransitionAssessment(
        status=status,
        summary="current Statement state",
        established_facts=["current frame is available"],
        open_gaps=[gap] if status == "in_progress" else [],
        last_action_effect="none",
    )


def _act(
    *,
    family: str = "activate",
    control: str = "Target",
    value: str = "",
    target_ref: str = "",
    role: str = "prepare",
) -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_assessment("in_progress"),
        kind="act",
        reason="the contract remains open",
        action=_TransitionAction(
            instruction=f"在当前界面中对 {control} 执行 {family}",
            atomic_role=role,
            action_family=family,
            target_control=control,
            target_value=value,
            target_ref=target_ref,
            expected_result="the target reflects the requested operation",
        ),
    )


def _complete(reason: str = "the target is visible") -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_assessment("satisfied"),
        kind="complete",
        reason=reason,
        evidence=[_TransitionEvidence(source="current_observation", claim=reason)],
    )


def _policy(statement: StatementContract) -> StatementSupervisorPolicy:
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id=INSTANCE)
    return policy


def _lookup_statement(
    entity: str = "Records",
    field: str = "name",
    required_fields: list[str] | None = None,
) -> StatementContract:
    return StatementContract(
        id="lookup",
        goal="resolve a collection",
        success="one collection is exposed",
        interaction_intent=CollectionIntent(
            phase="locate",
            entity=entity,
            field=field,
            required_fields=list(required_fields or []),
        ),
    )


def _run_lookup(monkeypatch, statement, transition, **observation):
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", transition)
    return policy._run_single_turn(statement, _observation(**observation), [])


def _observation(**updates) -> Observation:
    return Observation.model_validate({
        "png_bytes": _png(),
        "source": "browser",
        "title": "Current page",
        **updates,
    })


def test_transition_preserves_complete_visual_semantic_instruction(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter attributes",
        success="Attribute Code=size is applied",
        required_values={"Attribute Code": "size"},
    )
    policy = _policy(statement)
    decision = _act(
        family="input",
        control="Attribute Code",
        value="size",
        role="write",
    )
    instruction = (
        "在 Product Attributes 表格筛选区的 Attribute Code 输入框中填写 size，"
        "不要操作页面顶部的全局搜索框"
    )
    decision.action.instruction = instruction
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: decision)

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.action_intent.instruction == instruction
    assert step.action_intent.expected_result == "the target reflects the requested operation"


def test_offscreen_declared_subject_rejects_unrelated_write_and_replans_to_iterate(
    monkeypatch,
) -> None:
    statement = StatementContract(
        id="s1",
        goal="add one configuration",
        success="the configuration is saved",
        required_values={"Configurations": [{"Color": "green", "Size": "XXXL"}]},
    )
    policy = _policy(statement)
    decisions = iter([
        _act(
            family="select",
            control="Size",
            value="XXXL",
            role="write",
        ),
        _act(
            family="iterate",
            control="Configurations",
            role="iterate",
        ),
    ])
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *args, **kwargs: next(decisions),
    )
    observation = _observation(
        form_controls=[
            {"kind": "native_select", "label": "Size", "value": ""},
            {
                "kind": "section_toggle",
                "label": "Configurations",
                "in_viewport": False,
                "viewport_pos": "below",
            },
        ],
    )

    step = policy._run_single_turn(statement, observation, [])

    assert step.action_intent is not None
    assert step.action_intent.family == "iterate"
    assert step.action_intent.target_control == "Configurations"


def test_declared_write_binds_to_leading_projected_form_unit(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="add one member",
        success="the member is saved",
        required_values={
            "Admin Description": "XXXL",
            "Admin Swatch": "XXXL",
        },
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="input",
            control="Admin Description input in the new row",
            value="XXXL",
            role="write",
        ),
    )
    controls = [
        {
            "kind": "text_input",
            "label": label,
            "name": f"{label.lower()}[{index}]",
            "value": None,
            "group_id": f"collection:{index}",
            "group_index": index,
            "group_field": "Admin",
        }
        for index in (20, 21)
        for label in ("Description", "Swatch")
    ]

    step = policy._run_single_turn(
        statement,
        _observation(form_controls=controls),
        [],
    )

    assert step.action_intent is not None
    assert step.action_intent.target_ref == "description[21]"


def test_new_statement_receives_only_the_closed_predecessor_handoff(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="open the next resource",
        success="the next resource is visible",
    )
    policy = _policy(statement)
    captured = None

    def decide(*_args, memory_view, **_kwargs):
        nonlocal captured
        captured = memory_view.previous_statement
        return _act()

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    previous = StatementOutcomeEvent(
        statement_instance_id="run:previous",
        statement_id="s1",
        outcome=StatementOutcome.completed("the previous edit was saved"),
    )

    policy._run_single_turn(statement, _observation(), [previous])

    assert captured == {
        "status": "closed",
        "statement_id": "s1",
        "outcome": "completed",
    }


def test_target_handoff_mechanically_relocates_until_exact_target_is_visible(
    monkeypatch,
) -> None:
    target = {"name": "requested.zip"}
    statement = StatementContract(
        id="open",
        goal="open the requested archive",
        success="the archive is open",
        inputs={"target": target},
        observe_fields=["content"],
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail("relocation direction is deterministic"),
    )
    previous = StatementOutcomeEvent(
        statement_instance_id="run:collect",
        statement_id="collect",
        outcome=StatementOutcome.completed(
            "complete collection",
            outputs={"rows": [{"name": target["name"]}]},
            context_reports=[{
                "kind": "collection_cursor",
                "boundary": "end",
                "direction": "forward",
            }],
        ),
    )
    observation = _observation(collection_regions=[{
        "ref": "android-collection:files",
        "surface_fingerprint": "android-collection:files",
        "traversal": {"type": "scroll"},
        "cells": [{
            "ref": "android:files.0",
            "structural_key": "file",
            "content_key": "other.zip",
            "texts": ["other.zip"],
        }],
    }])

    step = policy._run_single_turn(statement, observation, [previous])

    assert step.action_intent is not None
    assert step.action_intent.family == "iterate"
    assert step.action_intent.direction == "up"
    assert "requested.zip" in step.action_intent.instruction


def test_offscreen_action_gets_one_same_frame_transition_retry(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="submit filter",
        success="filter submitted",
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _act(family="activate", control="Search", target_ref="42")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    observation = _observation(semantic_tree=[{
        "role": "button",
        "key": "Search",
        "ref": 42,
        "in_viewport": False,
    }])

    step = policy._run_single_turn(statement, observation, [])

    assert calls == 2
    assert step.outcome is None
    assert step.retry_transition is True
    assert "does not support operation 'activate'" in step.summary
    assert policy._last_transition_record["validation_error"]


def test_wrong_target_ref_gets_one_same_frame_transition_retry(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="open products",
        success="Products visible",
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _act(family="activate", control="Products", target_ref="99")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    step = policy._run_single_turn(
        statement,
        _observation(semantic_tree=[{
            "role": "link",
            "key": "Products",
            "ref": 11,
            "in_viewport": True,
        }]),
        [],
    )

    assert calls == 2
    assert step.outcome is None
    assert step.retry_transition is True
    assert "target_ref" in step.summary


@pytest.mark.parametrize("target_ref", ["WACSU99", "status"])
def test_native_select_id_and_name_are_the_same_select_affordance(
    monkeypatch, target_ref
) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter completed orders",
        success="only completed orders are visible",
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: _act(
            family="select",
            control="Status",
            value="Complete",
            target_ref=target_ref,
        ),
    )

    step = policy._run_single_turn(
        statement,
        _observation(form_controls=[{
            "kind": "native_select",
            "label": "notice-WACSU99",
            "name": "status",
            "id": "WACSU99",
            "options": ["Pending", "Complete"],
            "rect": {"x": 856, "y": 526, "w": 246, "h": 32},
        }]),
        [],
    )

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.family == "select"
    assert step.action_intent.target_value == "Complete"


def test_runtime_does_not_second_guess_action_semantics_from_strings(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="expose priority",
        success="priority is visible",
        required_values={"semantic status": "Active"},
        observe_fields=["Priority"],
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="select",
            control="Priority",
            value="High",
            role="prepare",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.target_control == "Priority"
    assert step.action_intent.target_value == "High"


def test_complete_rejects_inherited_filter_outside_declared_scope(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter records by quantity",
        success="only quantity 3 records remain",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Records",
            predicates=compile_filter_predicates({"Quantity": "3 - 3"}),
        ),
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail("structured reset must bypass Transition"),
    )

    step = policy._run_single_turn(
        statement,
        _observation(
            applied_filters={"Keyword": "old", "Quantity": "3 - 3"},
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({
                    "Keyword": "old",
                    "Quantity": "3 - 3",
                }),
                coverage="complete",
                source="test",
            ),
            form_control_state=[{
                "kind": "button",
                "label": "Clear all",
                "id": "reset",
                "query_action": "reset",
            }],
        ),
        [],
    )

    assert step.action_intent is not None
    assert step.action_intent.target_control == "Clear all"


def test_canonical_filter_acceptance_completes_without_transition(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter records by quantity",
        success="only quantity 3 records remain",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Records",
            predicates=compile_filter_predicates({"Quantity": 3}),
        ),
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail(
            "typed acceptance must bypass Transition"
        ),
    )

    step = policy._run_single_turn(
        statement,
        _observation(
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates(
                    {"Quantity": "3 - 3"},
                    display_numeric_ranges=True,
                ),
                coverage="complete",
                source="chips",
            ),
        ),
        [],
    )

    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.outcome.verification == "confirmed"


def test_filter_acceptance_mismatch_keeps_statement_running(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter records by quantity",
        success="only quantity 3 records remain",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Records",
            predicates=compile_filter_predicates({"Quantity": 3}),
        ),
    )
    policy = _policy(statement)
    calls = 0

    def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _complete("the filter looks active")

    monkeypatch.setattr(policy, "_invoke_statement_transition", complete)

    step = policy._run_single_turn(
        statement,
        _observation(
            applied_filter_state=AppliedFilterState(
                predicates=compile_filter_predicates({"Quantity": 4}),
                coverage="complete",
                source="test",
            ),
        ),
        [],
    )

    assert calls == 2
    assert step.outcome is None
    assert step.retry_transition is True


def test_staged_query_activates_matching_submit_before_pagination(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="locate the exact owner",
        success="the exact owner is open",
        inputs={"SKU": "WS08"},
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail("staged submission must bypass Transition"),
    )
    prior = PolicyTurn(
        index=1,
        observation_source="browser",
        statement_instance_id=INSTANCE,
        executed=True,
        supervisor=SupervisorStep(
            action_intent=ActionIntent(
                instruction="type the query",
                role="prepare",
                family="input",
                target_control="Search by keyword",
                target_value="WS08",
            ),
            summary="query entered",
            statement_id="s1",
        ),
    )

    step = policy._run_single_turn(
        statement,
        _observation(
            applied_filters=None,
            form_control_state=[{
                "kind": "text_input",
                "label": "Search by keyword",
                "value": "WS08",
                "is_filter": True,
            }],
            semantic_tree=[{
                "role": "button",
                "key": "Search",
                "ref": 42,
                "in_viewport": True,
                "query_action": "submit",
            }],
        ),
        [prior],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "activate"
    assert step.action_intent.target_control == "Search"
    assert step.action_intent.target_ref == "42"


def test_staged_filter_uses_typed_predicate_not_visual_input_text(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter reviews",
        success="the product filter is active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="All Reviews",
            predicates=compile_filter_predicates({
                "Product": "Olivia zip jacket",
            }),
        ),
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail("structured state must bypass Transition"),
    )
    prior = PolicyTurn(
        index=1,
        observation_source="browser",
        statement_instance_id=INSTANCE,
        executed=True,
        supervisor=SupervisorStep(
            action_intent=ActionIntent(
                instruction="type the product",
                role="prepare",
                family="input",
                target_control="Product filter input field",
                target_value="olivia zip jacket",
            ),
            summary="query entered",
            statement_id="s1",
        ),
    )

    step = policy._run_single_turn(
        statement,
        _observation(
            applied_filters=None,
            form_control_state=[{
                "kind": "text_input",
                "label": "Product",
                "name": "name",
                "group_field": "Product",
                "value": "olivia zip jacket",
                "is_filter": True,
            }],
            semantic_tree=[{
                "role": "button",
                "key": "Search",
                "ref": 42,
                "in_viewport": True,
                "query_action": "submit",
            }],
        ),
        [prior],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "activate"
    assert step.action_intent.target_control == "Search"


def test_constrain_replaces_mismatched_filter_from_typed_dom_state(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="filter reviews",
        success="the product filter is active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="All Reviews",
            predicates=compile_filter_predicates({"Product": "Olivia"}),
        ),
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: pytest.fail("structured write must bypass Transition"),
    )

    step = policy._run_single_turn(
        statement,
        _observation(form_control_state=[{
            "kind": "text_input",
            "label": "Product",
            "id": "reviewGrid_filter_name",
            "group_field": "Product",
            "value": "Olivia zip jacket",
            "is_filter": True,
        }]),
        [],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "input"
    assert step.action_intent.target_value == "olivia"
    assert step.action_intent.target_ref == "reviewGrid_filter_name"


def test_regular_form_input_does_not_trigger_mechanical_submission(monkeypatch) -> None:
    statement = StatementContract(id="s1", goal="update status", success="status saved")
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _act(family="activate", control="Save Status")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    prior = PolicyTurn(
        index=1,
        observation_source="browser",
        statement_instance_id=INSTANCE,
        executed=True,
        supervisor=SupervisorStep(
            action_intent=ActionIntent(
                instruction="type status",
                role="write",
                family="input",
                target_control="Status",
                target_value="Active",
            ),
            summary="status entered",
            statement_id="s1",
        ),
    )

    policy._run_single_turn(
        statement,
        _observation(
            form_control_state=[{
                "kind": "text_input", "label": "Status", "value": "Active",
            }],
            semantic_tree=[{
                "role": "button", "key": "Save Status", "in_viewport": True,
            }],
        ),
        [prior],
    )

    assert calls == 1


def test_query_only_lookup_returns_structural_scope_on_complete(monkeypatch) -> None:
    statement = _lookup_statement("Top Search Terms")
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: pytest.fail("lookup must complete mechanically"),
        url="https://example.test/dashboard",
        tables=[{
            "path": "#terms",
            "caption": "Top Search Terms",
            "headers": ["Search Term", "Uses"],
            "rows": [{"Search Term": "bag", "Uses": "3"}],
        }],
    )

    assert step.outcome is not None
    assert step.outcome.is_completed
    assert step.outcome.outputs["scope"]["surface_fingerprint"] == "table:#terms"


@pytest.mark.parametrize(
    ("control", "role"),
    [
        ("Delete Search", "commit"),
        ("Save Search", "prepare"),
    ],
)
def test_query_proposal_does_not_infer_permissions_from_control_label(
    monkeypatch, control, role,
) -> None:
    statement = _lookup_statement()
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: _act(
            family="activate",
            control=control,
            role=role,
        ),
        semantic_tree=[{
            "role": "button",
            "key": control,
            "ref": 9,
            "in_viewport": True,
        }],
    )

    # Proposal materialization is label-agnostic; labels are not a permission gate.
    assert step.action_intent is not None


def test_query_only_lookup_allows_structural_filter_input(monkeypatch) -> None:
    statement = _lookup_statement("Sahara", "Name")
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: _act(
            family="input",
            control="Name",
            value="Sahara",
            role="write",
        ),
        form_controls=[{
            "kind": "text_input",
            "label": "Name",
            "is_filter": True,
            "in_viewport": True,
        }],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "input"
    assert step.action_intent.target_value == "Sahara"


def test_query_only_lookup_allows_columns_control(monkeypatch) -> None:
    statement = _lookup_statement("Orders", required_fields=["Customer Email"])
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: _act(
            family="activate",
            control="Columns",
        ),
        semantic_tree=[{
            "role": "button",
            "key": "Columns",
            "ref": 9,
            "in_viewport": True,
        }],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "activate"


def test_query_only_lookup_matches_icon_prefixed_filter_control(monkeypatch) -> None:
    statement = _lookup_statement("Orders")
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: _act(
            family="activate",
            control="Filters",
        ),
        semantic_tree=[{
            "role": "button",
            "key": "\ue605Filters",
            "ref": 9,
            "in_viewport": True,
        }],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "activate"


@pytest.mark.parametrize("control", ["Cancel", "Clear All"])
def test_query_only_lookup_allows_local_query_presentation_controls(
    monkeypatch, control,
) -> None:
    statement = _lookup_statement("Orders")
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: _act(
            family="activate",
            control=control,
        ),
        semantic_tree=[{
            "role": "button",
            "key": control,
            "ref": 9,
            "in_viewport": True,
        }],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "activate"


def test_query_only_lookup_allows_required_column_toggle(monkeypatch) -> None:
    statement = _lookup_statement("Orders", required_fields=["Customer Email"])
    step = _run_lookup(
        monkeypatch,
        statement,
        lambda *_args, **_kwargs: _act(
            family="activate",
            control="Customer Email",
        ),
        semantic_tree=[{
            "role": "checkbox",
            "key": "Customer Email",
            "value": "false",
            "ref": 10,
            "in_viewport": True,
        }],
        form_controls=[{
            "kind": "checkbox_input",
            "label": "Customer Email",
            "id": "12",
            "value": "off",
        }],
    )

    assert step.action_intent is not None
    assert step.action_intent.family == "activate"


def test_invalid_structured_transition_retries_once(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="locate the exact account",
        success="the exact account is open",
        inputs={"Account Code": "account-east"},
    )
    policy = _policy(statement)
    decisions = iter([
        StructuredOutputError("failed transition requires cited evidence"),
        _act(family="activate", control="Back"),
    ])

    def transition(*_args, **_kwargs):
        decision = next(decisions)
        if isinstance(decision, Exception):
            raise decision
        return decision

    monkeypatch.setattr(policy, "_invoke_statement_transition", transition)

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.target_control == "Back"


def test_repeated_invalid_transition_keeps_statement_running(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="set the exact quantity",
        success="Quantity=3 is applied",
        required_values={"Quantity": "3"},
    )
    policy = _policy(statement)
    calls = 0

    def transition(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise StructuredOutputError("input transition requires target_value")

    monkeypatch.setattr(policy, "_invoke_statement_transition", transition)

    step = policy._run_single_turn(statement, _observation(), [])

    assert calls == 2
    assert step.outcome is None
    assert step.action_intent is None
    assert step.retry_transition is True
    assert "target_value" in step.summary
    assert policy._last_transition_record["validation_error"]


def test_adaptive_ui_field_name_is_allowed_when_value_matches_contract(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="set status",
        success="Status is Active",
        required_values={"semantic status": "Active"},
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="select",
            control="Current status",
            value="Active",
            role="write",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.action_intent is not None
    assert step.action_intent.target_control == "Current status"


def test_nested_required_value_is_allowed_for_range_field(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="set date range",
        success="Date range is applied",
        required_values={
            "date_range": {"from": "01/01/2023", "to": "05/31/2023"},
        },
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _act(
            family="input",
            control="Purchase Date From",
            value="01/01/2023",
            role="write",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.target_value == "01/01/2023"


def test_transition_completion_is_not_reinterpreted_by_a_hidden_persistence_fsm(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="save status",
        success="Status is saved",
        persistence="explicit_commit",
        required_values={"Status": "Active"},
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _complete("Status looks Active")

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    step = policy._run_single_turn(statement, _observation(), [])

    assert calls == 1
    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.pre_existing is True


def test_explicit_commit_rejects_completion_after_uncommitted_write(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="save status",
        success="Status is saved",
        persistence="explicit_commit",
    )
    policy = _policy(statement)
    decisions = iter([
        _complete("Status looks Active"),
        _act(family="activate", control="Save", role="commit"),
    ])
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: next(decisions),
    )
    write = PolicyTurn(
        index=1,
        observation_source="browser",
        statement_instance_id=INSTANCE,
        executed=True,
        supervisor=SupervisorStep(
            action_intent=ActionIntent(
                instruction="set status",
                role="write",
                family="select",
                target_control="Status",
                target_value="Active",
            ),
            summary="status set",
            statement_id="s1",
        ),
        action_signal=ActionSignal(
            role="write",
            execution="dispatched",
            target="unknown",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [write])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.role == "commit"


def test_explicit_commit_accepts_completion_after_later_commit(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="save status",
        success="Status is saved",
        persistence="explicit_commit",
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _complete("Status was submitted"),
    )
    history = [
        PolicyTurn(
            index=index,
            observation_source="browser",
            statement_instance_id=INSTANCE,
            executed=True,
            supervisor=SupervisorStep(
                action_intent=ActionIntent(
                    instruction=role,
                    role=role,
                    family="input" if role == "write" else "activate",
                ),
                summary=role,
                statement_id="s1",
            ),
            action_signal=ActionSignal(
                role=role,
                execution="dispatched",
                target="unknown",
            ),
        )
        for index, role in enumerate(("write", "commit"), start=1)
    ]

    step = policy._run_single_turn(statement, _observation(), history)

    assert step.outcome is not None
    assert step.outcome.phase == "completed"
    assert step.pre_existing is False


def test_commit_writeback_cannot_restart_preparation(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="save generated values",
        success="Generated values are saved",
        persistence="explicit_commit",
    )
    policy = _policy(statement)
    decisions = iter([
        _act(family="activate", control="Edit values", role="prepare"),
        _act(family="activate", control="Save", role="commit"),
    ])
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: next(decisions),
    )
    writeback = PolicyTurn(
        index=1,
        observation_source="browser",
        statement_instance_id=INSTANCE,
        executed=True,
        supervisor=SupervisorStep(
            action_intent=ActionIntent(
                instruction="apply generated values",
                role="commit",
                family="activate",
                target_control="Apply",
            ),
            summary="values written back",
            statement_id="s1",
        ),
        action_signal=ActionSignal(
            role="write",
            execution="dispatched",
            target="on_target",
        ),
    )

    step = policy._run_single_turn(statement, _observation(), [writeback])

    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.role == "commit"
    assert step.action_intent.target_control == "Save"


def test_terminal_budget_does_not_replace_act_with_a_terminal_decision(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="continue editing",
        success="saved",
    )
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _act())

    step = policy.reconcile(_observation(), "goal", [])

    assert step.outcome is not None and step.outcome.phase == "exhausted"
    assert "hard-budget final frame" in step.outcome.summary


def test_valid_completion_uses_runtime_evidence_grade(monkeypatch) -> None:
    statement = StatementContract(
        id="s1",
        goal="reach target page",
        success="Target page is visible",
    )
    policy = _policy(statement)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: _complete())

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is not None and step.outcome.phase == "completed"
    assert step.outcome.verification == "confirmed"
    assert policy._last_transition_record["validation_error"] == ""


def test_rich_reach_state_does_not_complete_from_collection_identity_alone(
    monkeypatch,
) -> None:
    statement = StatementContract(
        id="s1",
        goal="configure and show the Orders report",
        success="every declared expected-state condition is established",
        expected_state={
            "entity": "Sales Reports",
            "Report Subtype": "Orders",
            "From": "05/01/2021",
            "To": "03/31/2022",
            "rendered": True,
        },
        interaction_intent=CollectionIntent(
            phase="reach",
            entity="Sales Reports",
        ),
    )
    policy = _policy(statement)
    calls = 0

    def decide(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _act(
            family="input",
            control="From",
            value="05/01/2021",
        )

    monkeypatch.setattr(policy, "_invoke_statement_transition", decide)
    step = policy._run_single_turn(
        statement,
        _observation(tables=[{
            "path": "#sales-report",
            "caption": "Sales Reports",
            "headers": ["Order"],
            "rows": [{"Order": "1"}],
        }]),
        [],
    )

    assert calls == 1
    assert step.outcome is None
    assert step.action_intent is not None
    assert step.action_intent.target_control == "From"
    assert step.action_intent.target_value == "05/01/2021"


def test_visual_reach_state_completes_without_a_structural_collection(
    monkeypatch,
) -> None:
    statement = StatementContract(
        id="s1",
        goal="show the requested visible result",
        success="every declared expected-state condition is established",
        expected_state={
            "entity": "VisibleResult",
            "fields": ["value"],
        },
    )
    policy = _policy(statement)
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *_args, **_kwargs: _complete("the requested value is visible"),
    )

    step = policy._run_single_turn(statement, _observation(), [])

    assert step.outcome is not None
    assert step.outcome.is_completed
    assert step.outcome.outputs == {}
    assert policy._last_transition_record["validation_error"] == ""


def test_structurally_invalid_reach_complete_keeps_statement_running(
    monkeypatch,
) -> None:
    statement = StatementContract(
        id="s1",
        goal="open the Items collection",
        success="the Items collection is established",
        expected_state={"entity": "Items"},
        interaction_intent=CollectionIntent(
            phase="reach",
            entity="Items",
        ),
    )
    policy = _policy(statement)
    calls = 0

    def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _complete("an Item Attributes collection is visible")

    monkeypatch.setattr(policy, "_invoke_statement_transition", complete)
    step = policy._run_single_turn(
        statement,
        _observation(tables=[{
            "path": "#item-attributes",
            "caption": "Item Attributes",
            "headers": ["Attribute"],
            "rows": [{"Attribute": "Color"}],
        }]),
        [],
    )

    assert calls == 2
    assert step.outcome is None
    assert step.retry_transition is True
    assert "requested structural collection" in step.summary
