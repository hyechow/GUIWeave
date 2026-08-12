from __future__ import annotations

from datetime import datetime

import pytest

from gui_agent.core.tool_agent.data_normalization import (
    ValueNormalizationError,
    json_value,
    normalize_table_value,
)


def test_auto_normalization_handles_dates_money_and_numeric_fields() -> None:
    assert normalize_table_value("Purchase Date", "Jun 9, 2023 9:00:00 AM") == (
        "2023-06-09T09:00:00+00:00"
    )
    assert normalize_table_value("Grand Total", "$1,234.50") == 1234.5
    assert normalize_table_value("Uses", "19") == 19
    assert normalize_table_value("ID", "000062") == "000062"


def test_declared_types_are_lossless_and_json_serializable() -> None:
    parsed = normalize_table_value(
        "modified_at",
        "Jul 11",
        "datetime",
    )

    assert parsed == datetime.fromisoformat("1900-07-11 00:00:00+00:00")
    assert json_value(parsed) == "1900-07-11T00:00:00+00:00"
    assert normalize_table_value("amount", "￥ 367 .25", "money") == 367.25


def test_invalid_declared_number_is_rejected() -> None:
    with pytest.raises(ValueNormalizationError, match="cannot parse"):
        normalize_table_value("amount", "about twelve", "number")
