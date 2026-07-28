"""Transition context contains decision evidence, not the complete adapter indexes."""

from __future__ import annotations

import json

from gui_agent.core.filter_contract import compile_filter_predicates
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import CollectionIntent, Observation, StatementContract
from gui_agent.core.supervisor.statement.context_projection import (
    declared_target_affordances,
    project_transition_frame,
)
from gui_agent.core.supervisor.statement.observation_view import build_observation_view


def _frame(
    statement: StatementContract,
    observation: Observation,
    history: list | None = None,
    previous_statement: dict[str, str] | None = None,
) -> dict:
    turns = history or []
    view = build_observation_view(statement, observation, turns)
    return project_transition_frame(
        statement,
        observation,
        build_memory_view(
            instance_id="i1",
            contract=statement,
            history=turns,
            previous_statement=previous_statement,
        ),
        view,
        initial_filters=None,
    )


def test_closed_predecessor_is_projected_separately_from_active_memory() -> None:
    statement = StatementContract(
        id="s2",
        goal="open the next resource",
        success="the next resource is visible",
    )
    frame = _frame(
        statement,
        Observation(png_bytes=b"x", source="browser"),
        previous_statement={
            "status": "closed",
            "statement_id": "s1",
            "outcome": "completed",
        },
    )

    assert frame["handoff"]["statement_id"] == "s1"
    assert frame["handoff"]["status"] == "closed"
    assert frame["memory"]["durable_facts"] == []


def _table() -> dict:
    return {
        "index": 1,
        "source": "table",
        "caption": "Products",
        "headers": ["Name", "Quantity", "Action"],
        "rows": [{"Name": "secret row", "Quantity": "99", "Action": "Edit"}],
        "row_count": 1,
        "total_records": 2000,
        "partial": True,
        "page": {"title": "Products"},
        "traversal": {"type": "paged", "has_next_page": True},
        "path": "large adapter-only selector",
    }


def test_constrain_projects_filter_controls_and_collection_shape_without_rows() -> None:
    statement = StatementContract(
        id="c1",
        goal="Narrow Products to Quantity=3",
        success="The exact predicate is active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Products",
            predicates=compile_filter_predicates({"Quantity": 3}),
        ),
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        title="Products",
        tables=[_table()],
        form_control_state=[
            {"kind": "button", "label": "Filters"},
            {
                "kind": "text_input",
                "label": "from",
                "name": "qty[from]",
                "is_filter": True,
            },
            {
                "kind": "text_input",
                "label": "to",
                "name": "qty[to]",
                "is_filter": True,
            },
            {
                "kind": "button",
                "label": "Apply Filters",
                "query_action": "submit",
            },
            {
                "kind": "checkbox_input",
                "label": "row-1",
                "value": "on",
                "group_id": "grid:0",
                "group_field": "Selection",
            },
        ],
    )

    frame = _frame(statement, observation)
    projected = frame["observation"]

    assert [
        item["field"]
        for item in projected["form_units"][0]["fields"]
    ] == [
        "from",
        "to",
        "Apply Filters",
    ]
    assert {item["label"] for item in projected["affordances"]} == {
        "from",
        "to",
        "Apply Filters",
    }
    assert projected["tables"][0]["headers"] == ["Name", "Quantity", "Action"]
    assert "rows" not in projected["tables"][0]
    assert "path" not in projected["tables"][0]
    assert "secret row" not in json.dumps(projected)


def test_constrain_matches_declared_targets_from_typed_predicate_keys() -> None:
    statement = StatementContract(
        id="c1",
        goal="Narrow Products by name and type",
        success="The exact predicates are active",
        interaction_intent=CollectionIntent(
            phase="constrain",
            entity="Products",
            predicates=compile_filter_predicates({
                "Name": "Minerva LumaTech V-Tee",
                "Type": "Configurable Product",
            }),
        ),
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        form_control_state=[
            {
                "kind": "text_input",
                "label": "Name",
                "in_viewport": True,
            },
            {
                "kind": "native_select",
                "label": "Type",
                "in_viewport": True,
            },
        ],
    )
    view = build_observation_view(statement, observation, [])

    targets = declared_target_affordances(statement, view)

    assert {item["label"] for item in targets} == {"Name", "Type"}


def test_reach_keeps_navigation_affordances_without_form_state_or_table_rows() -> None:
    statement = StatementContract(
        id="r1",
        goal="Reach Products",
        success="Products collection is visible",
        interaction_intent=CollectionIntent(
            phase="reach",
            entity="Products",
            required_fields=["Name", "Quantity"],
        ),
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        title="Dashboard",
        semantic_tree=[
            {
                "role": "link",
                "key": "Catalog",
                "ref": 1,
                "in_viewport": True,
                "url": "https://example.test/catalog",
            },
        ],
        form_control_state=[
            {"kind": "text_input", "label": "global search", "value": ""},
        ],
        tables=[_table()],
    )

    projected = _frame(statement, observation)["observation"]

    assert projected["form_units"] == []
    assert projected["affordances"][0]["label"] == "Catalog"
    assert "rows" not in projected["tables"][0]


def test_untyped_statement_keeps_bounded_field_state_but_not_row_or_button_duplicates() -> None:
    statement = StatementContract(
        id="w1",
        goal="Update the description",
        success="The form is saved",
        required_values={"Description": "new text"},
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        form_controls=[
            {"kind": "text_input", "label": "Description", "value": "old text"},
            {"kind": "button", "label": "Save"},
            {
                "kind": "checkbox_input",
                "label": "row-1",
                "value": "on",
                "group_id": "grid:0",
                "group_field": "Selection",
            },
        ],
        form_control_state=[
            {
                "kind": "text_input",
                "label": "Description",
                "value": "old text",
                "in_viewport": False,
            },
            {"kind": "button", "label": "Save"},
            {
                "kind": "checkbox_input",
                "label": "row-1",
                "value": "on",
                "group_id": "grid:0",
                "group_field": "Selection",
            },
        ],
    )

    projected = _frame(statement, observation)["observation"]

    form = projected["form_units"][0]
    assert form["id"] == "__form__"
    assert form["fields"][0]["field"] == "Description"
    assert form["fields"][0]["value"] == "old text"
    assert {item["label"] for item in projected["affordances"]} == {
        "Save",
        "row-1",
    }
    assert next(
        item for item in projected["affordances"] if item["label"] == "row-1"
    )["value"] == "on"


def test_untyped_statement_does_not_treat_offscreen_table_rows_as_current_view() -> None:
    statement = StatementContract(
        id="w1",
        goal="Add one configuration",
        success="The configuration is saved",
        required_values={"Configurations": [{"Color": "green", "Size": "XXXL"}]},
    )
    table = {
        **_table(),
        "in_viewport": False,
        "viewport_pos": "below",
    }
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        tables=[table],
        form_controls=[
            {
                "kind": "native_select",
                "label": "Size",
                "value": "",
            },
            {
                "kind": "button",
                "label": "Add Attribute",
            },
            {
                "kind": "section_toggle",
                "label": "Configurations",
                "in_viewport": False,
                "viewport_pos": "below",
            },
        ],
    )

    projected = _frame(statement, observation)["observation"]

    assert projected["tables"][0]["visibility"] == "offscreen"
    assert projected["tables"][0]["viewport_pos"] == "below"
    assert "rows" not in projected["tables"][0]
    assert projected["form_units"] == []
    assert projected["declared_targets"][0]["label"] == "Configurations"
    assert projected["declared_targets"][0]["visibility"] == "offscreen"
    assert projected["affordances"][0]["label"] == "Configurations"
    assert projected["affordances"][0]["supported_operations"] == ["iterate"]


def test_untyped_statement_keeps_rows_for_visible_table() -> None:
    statement = StatementContract(
        id="w1",
        goal="Update one visible row",
        success="The row is saved",
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        tables=[{**_table(), "in_viewport": True, "viewport_pos": "in"}],
    )

    projected = _frame(statement, observation)["observation"]

    assert projected["tables"][0]["visibility"] == "visible"
    assert projected["tables"][0]["rows"][0]["Name"] == "secret row"


def test_mutation_projects_bounded_relevant_dynamic_form_groups() -> None:
    statement = StatementContract(
        id="w2",
        goal="Add one member to the collection",
        success="The form is saved",
        required_values={
            "Admin Description": "XXXL",
            "Admin Swatch": "XXXL",
        },
    )
    controls = []
    for index in range(1, 10):
        value = None if index == 9 else f"value-{index}"
        for group_field in ("Admin", "Default Store View"):
            controls.extend([
                {
                    "kind": "text_input",
                    "label": "Description",
                    "name": f"description[{index}][{group_field}]",
                    "value": value,
                    "group_id": f"collection:{index}",
                    "group_index": index,
                    "group_field": group_field,
                },
                {
                    "kind": "text_input",
                    "label": "Swatch",
                    "name": f"swatch[{index}][{group_field}]",
                    "value": value,
                    "group_id": f"collection:{index}",
                    "group_index": index,
                    "group_field": group_field,
                },
            ])
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        form_control_state=controls,
    )

    projected = _frame(statement, observation)["observation"]

    group_ids = {item["id"] for item in projected["form_units"]}
    assert "collection:9" in group_ids
    assert len(group_ids) == 4
    assert "collection:1" not in group_ids
    assert projected["form_units"][0]["id"] == "collection:9"
    new_unit = next(
        item for item in projected["form_units"]
        if item["id"] == "collection:9"
    )
    assert {
        item["field"]
        for item in new_unit["fields"]
    } == {
        "Admin Description",
        "Admin Swatch",
    }
    assert not [
        item
        for item in projected["affordances"]
        if item["role"] == "text_input"
    ]


def test_projection_preserves_canonical_offscreen_affordance_index() -> None:
    statement = StatementContract(
        id="r1",
        goal="Expose requested business fields",
        success="The requested values can be observed",
        observe_fields=["Material"],
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        form_controls_meta={"coverage": "complete"},
        semantic_tree=[
            {
                "role": "button",
                "key": "Back",
                "ref": "back",
                "in_viewport": True,
            },
            {
                "role": "combobox",
                "key": "[global] Material",
                "ref": "material",
                "in_viewport": False,
            },
            *[
                {
                    "role": "textbox",
                    "key": f"Irrelevant field {index}",
                    "ref": f"field-{index}",
                    "in_viewport": False,
                }
                for index in range(300)
            ],
        ],
    )

    projected = _frame(statement, observation)["observation"]

    assert len(projected["affordances"]) == 302
    assert projected["affordance_coverage"] == "complete"
