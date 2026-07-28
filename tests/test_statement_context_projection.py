"""Transition context contains decision evidence, not the complete adapter indexes."""

from __future__ import annotations

import json

from gui_agent.core.filter_contract import compile_filter_predicates
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import CollectionIntent, Observation, StatementContract
from gui_agent.core.supervisor.statement.context_projection import transition_frame_block
from gui_agent.core.supervisor.statement.observation_view import build_observation_view


def _frame(statement: StatementContract, observation: Observation) -> dict:
    view = build_observation_view(statement, observation, [])
    block = transition_frame_block(
        statement,
        observation,
        build_memory_view(
            instance_id="i1",
            contract=statement,
            history=[],
        ),
        view,
        initial_filters=None,
    )
    return json.loads(block.content[block.content.index("{"):])


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
                "group_id": "grid:0",
                "group_field": "Selection",
            },
        ],
    )

    frame = _frame(statement, observation)
    projected = frame["observation"]

    assert [item["label"] for item in projected["control_state"]] == [
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

    assert projected["control_state"] == []
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
                "group_id": "grid:0",
                "group_field": "Selection",
            },
        ],
    )

    projected = _frame(statement, observation)["observation"]

    assert projected["control_state"] == [{
        "kind": "text_input",
        "label": "Description",
        "value": "old text",
    }]
    assert {item["label"] for item in projected["affordances"]} == {
        "Description",
        "Save",
        "row-1",
    }


def test_transition_projects_offscreen_affordances_by_structured_contract_field() -> None:
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

    assert [item["label"] for item in projected["affordances"]] == [
        "Back",
        "[global] Material",
    ]
    assert projected["affordance_coverage"] == "partial"
