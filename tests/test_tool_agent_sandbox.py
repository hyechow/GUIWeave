from __future__ import annotations

import pytest

from gui_agent.core.tool_agent.sandbox import (
    TransformValidationError,
    execute_transform,
    validate_transform_row_fields,
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


def test_transform_can_guard_canonical_values_by_safe_runtime_type() -> None:
    source = """def transform(rows):
    return [row["value"] for row in rows if isinstance(row["value"], str)]
"""

    result = execute_transform(
        source,
        [{"value": "2023-01-01T00:00:00+00:00"}, {"value": 1}],
        {"type": "array", "items": {"type": "string"}},
    )

    assert result == ["2023-01-01T00:00:00+00:00"]


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


def test_transform_reads_normalized_schema_fields_not_display_headers() -> None:
    schema = {
        "type": "object",
        "properties": {"customer_email": {"type": "string"}},
        "required": ["customer_email"],
    }
    normalized = (
        "def transform(rows):\n"
        "    return [row.get('customer_email', '') for row in rows]"
    )
    display_header = normalized.replace("customer_email", "Customer Email")

    validate_transform_row_fields(normalized, schema)
    with pytest.raises(TransformValidationError, match="Customer Email"):
        validate_transform_row_fields(display_header, schema)


def test_transform_rejects_undeclared_free_text_enum_comparison() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string"},
        },
    }
    guessed_enum = (
        "def transform(inputs):\n"
        "    rows = inputs[0]\n"
        "    return [row for row in rows if row['kind'].lower() == 'zip']"
    )
    structural = guessed_enum.replace(
        "row['kind'].lower() == 'zip'", "row['name'].lower().endswith('.zip')"
    )

    with pytest.raises(TransformValidationError, match="free-form text field 'kind'"):
        validate_transform_row_fields(guessed_enum, schema)
    validate_transform_row_fields(structural, schema)
    validate_transform_row_fields(
        guessed_enum, schema, literal_filters={"kind": "zip"},
    )


def test_transform_timeout_remains_enforced_with_live_runtime_startup_budget() -> None:
    source = "def transform(rows):\n    for _ in range(1000000000000):\n        pass\n    return []"

    with pytest.raises(TimeoutError, match="exceeded"):
        execute_transform(
            source,
            [],
            {"type": "array"},
            timeout_s=0.05,
        )
