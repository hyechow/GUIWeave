"""Typed normalization for values extracted from visual tables."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, JsonValue


ValueType = Literal["auto", "text", "number", "money", "datetime", "boolean"]


class ValueNormalizationError(ValueError):
    """A declared field type could not be normalized losslessly."""


def json_value(value: Any) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_value(item) for item in sorted(value, key=repr)]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


_CURRENCY_RE = re.compile(r"[$€£¥￥₹₩₪₫฿₽₺₦₱]")
_NUMBER_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_NUMERIC_FIELD_WORDS = frozenset(
    "amount balance cost count counts percent percentage price quantity quantities "
    "qty rank rating result results score subtotal total uses".split()
)
_DATETIME_FORMATS = (
    "%b %d, %Y %I:%M:%S %p",
    "%B %d, %Y %I:%M:%S %p",
    "%b %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M %p",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d",
    "%B %d",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def _decimal(value: Any, *, money: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        kind = "money" if money else "number"
        raise ValueNormalizationError(f"cannot parse {value!r} as {kind}")
    text = str(value).strip().replace("−", "-")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if money:
        text = _CURRENCY_RE.sub("", text)
        text = re.sub(r"(?<=\d)\s+(?=[.,]\d)", "", text)
    text = text.replace(",", "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        kind = "money" if money else "number"
        raise ValueNormalizationError(f"cannot parse {value!r} as {kind}") from exc
    return -result if negative else result


def _datetime(value: Any) -> datetime:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        raise ValueNormalizationError(f"cannot parse {value!r} as datetime")
    normalized = re.sub(r"\bSept\b", "Sep", text, flags=re.IGNORECASE)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _DATETIME_FORMATS:
            try:
                parsed = datetime.strptime(normalized, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueNormalizationError(f"cannot parse {value!r} as datetime")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _typed(value: Any, value_type: ValueType) -> Any:
    if value_type == "auto":
        return value
    if value_type == "text":
        return "" if value is None else str(value)
    if value_type == "number":
        return _decimal(value)
    if value_type == "money":
        return _decimal(value, money=True)
    if value_type == "datetime":
        return _datetime(value)
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"true", "yes", "1", "on", "是"}:
        return True
    if text in {"false", "no", "0", "off", "否"}:
        return False
    raise ValueNormalizationError(f"cannot parse {value!r} as boolean")


def normalize_table_value(
    field_name: str,
    value: Any,
    value_type: ValueType = "auto",
) -> Any:
    if value_type == "datetime":
        return _datetime(value)
    if value_type == "number":
        numbers = _NUMBER_RE.findall(str(value).strip())
        if len(numbers) != 1:
            raise ValueNormalizationError(f"cannot parse {value!r} as number")
        return json_value(_decimal(numbers[0]))
    if value_type == "money":
        return json_value(_decimal(value, money=True))
    if value_type != "auto":
        return json_value(_typed(value, value_type))
    if not isinstance(value, str):
        return json_value(value)
    text = value.strip()
    if _CURRENCY_RE.search(text):
        return json_value(_decimal(text, money=True))
    words = set(re.findall(r"[\w]+", field_name.casefold()))
    if words & {"date", "datetime", "time", "timestamp"}:
        try:
            return json_value(_datetime(text))
        except ValueNormalizationError:
            pass
    numbers = _NUMBER_RE.findall(text)
    if words & _NUMERIC_FIELD_WORDS and len(numbers) == 1:
        try:
            return json_value(_decimal(numbers[0]))
        except ValueNormalizationError:
            pass
    return text


__all__ = [
    "ValueNormalizationError",
    "ValueType",
    "json_value",
    "normalize_table_value",
]
