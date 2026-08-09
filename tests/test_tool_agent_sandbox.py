from __future__ import annotations

import pytest

from gui_agent.core.tool_agent.sandbox import (
    TransformValidationError,
    execute_transform,
    validate_transform_source,
)


def test_transform_processes_private_chunks_and_validates_result() -> None:
    source = """def transform(rows):
    ranked = sorted(rows, key=lambda row: row["metric"], reverse=True)
    return [row["label"] for row in ranked[:2]]
"""
    result = execute_transform(
        source,
        [{"label": "second", "metric": 2}, {"label": "first", "metric": 5}],
        {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    )

    assert result == ["first", "second"]


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef transform(rows):\n    return []",
        "def transform(rows):\n    return rows.__class__",
        "def transform(rows):\n    while True:\n        pass",
    ],
)
def test_transform_rejects_unsafe_source(source: str) -> None:
    with pytest.raises(TransformValidationError):
        validate_transform_source(source)
