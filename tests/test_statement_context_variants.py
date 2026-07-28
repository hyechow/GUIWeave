from __future__ import annotations

import json

from gui_agent.context import ContextCompressor
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.context_projection import (
    project_transition_observation,
)
from gui_agent.core.supervisor.statement.context_variants import (
    transition_frame_block,
)
from gui_agent.core.supervisor.statement.observation_view import build_observation_view


def _frame(affordances: list[dict], *, coverage: str = "complete") -> dict:
    return {
        "contract": {},
        "memory": {},
        "observation": {
            "title": "Example",
            "url": "https://example.test",
            "affordance_coverage": coverage,
            "affordances": affordances,
        },
    }


def _payload(content: str) -> dict:
    return json.loads(content[content.index("{"):])


def test_affordance_variant_uses_projection_relevance_without_exposing_it() -> None:
    frame = _frame([
        {
            "label": "Back",
            "visibility": "visible",
            "supported_operations": ["activate"],
            "_relevance": "current",
        },
        {
            "label": "[global] Material",
            "visibility": "offscreen",
            "supported_operations": ["iterate"],
            "_relevance": "contract_target",
        },
        *[
            {
                "label": f"Irrelevant field {index}",
                "visibility": "offscreen",
                "supported_operations": ["iterate"],
                "_relevance": "background",
            }
            for index in range(300)
        ],
    ])

    block = transition_frame_block(frame)
    full = _payload(block.content)
    compact = _payload(block.variants[0].content)

    assert len(full["observation"]["affordances"]) == 302
    assert [
        item["label"] for item in compact["observation"]["affordances"]
    ] == ["Back", "[global] Material"]
    assert compact["observation"]["affordance_coverage"] == "partial"
    assert "_relevance" not in block.content
    assert "_relevance" not in block.variants[0].content


def test_shared_compressor_selects_affordance_variant_only_when_needed() -> None:
    frame = _frame([
        {
            "label": "Back",
            "visibility": "visible",
            "_relevance": "current",
        },
        *[
            {
                "label": f"Background {index}",
                "visibility": "offscreen",
                "_relevance": "background",
            }
            for index in range(100)
        ],
    ])
    block = transition_frame_block(frame)

    roomy = ContextCompressor(max_chars=100_000).apply([block])
    tight = ContextCompressor(max_chars=1_000).apply([block])

    assert roomy.decisions[0].action == "kept"
    assert len(_payload(roomy.kept[0].content)["observation"]["affordances"]) == 101
    assert tight.decisions[0].action == "compressed"
    assert len(_payload(tight.kept[0].content)["observation"]["affordances"]) == 1
    assert tight.decisions[0].strategy == (
        "drop_background_offscreen_affordances"
    )


def test_projection_marks_contract_target_before_variant_generation() -> None:
    statement = StatementContract(
        id="r1",
        goal="Expose requested fields",
        success="Fields are visible",
        observe_fields=["Material"],
    )
    observation = Observation(
        png_bytes=b"x",
        source="browser",
        semantic_tree=[
            {
                "role": "combobox",
                "key": "[global] Material",
                "in_viewport": False,
            },
            {
                "role": "textbox",
                "key": "Unrelated",
                "in_viewport": False,
            },
        ],
    )
    view = build_observation_view(statement, observation, [])

    projected = project_transition_observation(
        statement,
        observation,
        view,
        initial_filters=None,
    )

    assert {
        item["label"]: item["_relevance"]
        for item in projected["affordances"]
    } == {
        "[global] Material": "contract_target",
        "Unrelated": "background",
    }
