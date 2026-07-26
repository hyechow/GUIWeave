"""Platform-neutral filter postconditions."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


FilterOperator = Literal["eq", "gte", "lte", "range"]
_LOWER = {"from", "start", "min"}
_UPPER = {"to", "end", "max"}
_BOUND_SUFFIX = re.compile(
    r"^(?P<field>.+?)(?:\s+|_)(?P<bound>from|start|min|to|end|max)$",
    re.IGNORECASE,
)
_DATE = r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
_DATE_RANGE = re.compile(rf"^\s*(?P<lower>{_DATE})\s+-\s+(?P<upper>{_DATE})\s*$")
_DATE_VALUE = re.compile(
    r"^(?P<a>\d{1,4})(?P<sep>[-/])(?P<b>\d{1,2})(?P=sep)(?P<c>\d{1,4})$"
)


def canonical_filter_field(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"\s+", " ", text.replace("_", " "))


def canonical_filter_value(value: JsonValue) -> JsonValue:
    if not isinstance(value, str):
        return value
    text = re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", value).strip().casefold()
    )
    match = _DATE_VALUE.fullmatch(text)
    if match is None:
        return text
    a, b, c = match.group("a"), match.group("b"), match.group("c")
    if len(a) == 4:
        year, month, day = a, b, c
    else:
        year = f"20{c}" if len(c) == 2 else c
        month, day = a, b
    return f"{year}-{int(month):02d}-{int(day):02d}"


class FilterPredicate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: FilterOperator = "eq"
    values: list[JsonValue]

    @model_validator(mode="after")
    def _valid_arity(self) -> "FilterPredicate":
        if len(self.values) != (2 if self.operator == "range" else 1):
            raise ValueError(f"invalid {self.operator} predicate")
        return self


FilterPredicateSet = dict[str, FilterPredicate]


class AppliedFilterState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    predicates: FilterPredicateSet = Field(default_factory=dict)
    coverage: Literal["unavailable", "partial", "complete"] = "unavailable"
    source: str = ""


def _bounds(value: Any) -> tuple[JsonValue | None, JsonValue | None] | None:
    if isinstance(value, str):
        match = _DATE_RANGE.fullmatch(value)
        return (
            (
                canonical_filter_value(match.group("lower")),
                canonical_filter_value(match.group("upper")),
            )
            if match
            else None
        )
    if not isinstance(value, dict) or not value:
        return None
    result: list[JsonValue | None] = [None, None]
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().casefold()
        index = 0 if key in _LOWER else 1 if key in _UPPER else -1
        if index < 0:
            return None
        normalized = canonical_filter_value(raw_value)
        if result[index] is not None and result[index] != normalized:
            raise ValueError("conflicting range bounds")
        result[index] = normalized
    return result[0], result[1]


def compile_filter_predicates(filters: dict[str, Any] | None) -> FilterPredicateSet:
    predicates: FilterPredicateSet = {}
    pending: dict[str, tuple[JsonValue | None, JsonValue | None]] = {}
    for raw_field, raw_value in (filters or {}).items():
        field = str(raw_field or "").strip()
        if not field:
            continue
        suffix = _BOUND_SUFFIX.fullmatch(field)
        key = canonical_filter_field(suffix.group("field") if suffix else field)
        if suffix:
            lower, upper = pending.get(key, (None, None))
            value = canonical_filter_value(raw_value)
            if suffix.group("bound").casefold() in _LOWER:
                lower = value
            else:
                upper = value
            pending[key] = lower, upper
            continue
        bounds = _bounds(raw_value)
        if bounds is not None:
            pending[key] = bounds
        else:
            predicates[key] = FilterPredicate(
                values=[canonical_filter_value(raw_value)]
            )

    for key, (lower, upper) in pending.items():
        if key in predicates:
            raise ValueError(f"filter field {key!r} mixes direct and range values")
        values = [value for value in (lower, upper) if value is not None]
        if values:
            operator = (
                "range"
                if len(values) == 2
                else "gte"
                if lower is not None
                else "lte"
            )
            predicates[key] = FilterPredicate(operator=operator, values=values)
    return dict(sorted(predicates.items()))


def compare_filter_state(
    requested: FilterPredicateSet,
    actual: AppliedFilterState | None,
) -> bool | None:
    if actual is None or actual.coverage != "complete":
        return None
    return requested == actual.predicates
