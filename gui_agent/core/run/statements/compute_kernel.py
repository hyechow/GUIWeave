"""Typed, side-effect-free transformations for Program Compute statements."""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, JsonValue

from gui_agent.core.data_types import (
    AggregateSpec,
    AggregateStep,
    ArithmeticStep,
    BuildRecordStep,
    ComputeStep,
    DateBucketStep,
    DistinctStep,
    FieldRef,
    FilterStep,
    GroupStep,
    ProjectStep,
    RankStep,
    SortKey,
    SortStep,
    TakeStep,
    TextSplitStep,
)


class ComputeKernelError(ValueError):
    """A typed data operation could not be applied to its runtime records."""


def json_value(value: Any) -> JsonValue:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


_CURRENCY_RE = re.compile(r"[$€£¥₹₩₪₫฿₽₺₦₱]")
_DATETIME_FORMATS = (
    "%b %d, %Y %I:%M:%S %p", "%B %d, %Y %I:%M:%S %p",
    "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p",
    "%b %d, %Y", "%B %d, %Y",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
)


def _decimal(value: Any, *, money: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ComputeKernelError(f"cannot parse {value!r} as {'money' if money else 'number'}")
    text = str(value).strip().replace("−", "-")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    if money:
        text = _CURRENCY_RE.sub("", text)
    text = text.replace(",", "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ComputeKernelError(f"cannot parse {value!r} as {'money' if money else 'number'}") from exc
    return -result if negative else result


def _datetime(value: Any) -> datetime:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        raise ComputeKernelError(f"cannot parse {value!r} as datetime")
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
        raise ComputeKernelError(f"cannot parse {value!r} as datetime")
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
    raise ComputeKernelError(f"cannot parse {value!r} as boolean")


def _resolve(value: Any, ref: FieldRef) -> Any:
    current = value
    for part in ref.path:
        if isinstance(part, str) and isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(part, int) and isinstance(current, list) and 0 <= part < len(current):
            current = current[part]
        else:
            raise ComputeKernelError(f"field path does not exist: {ref.path}")
    return _typed(current, ref.type)


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        value = value["rows"]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ComputeKernelError("transform source must be record list or table snapshot")
    return [dict(item) for item in value]


def _fingerprint(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _fingerprint(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_fingerprint(item) for item in value)
    if isinstance(value, Decimal):
        return ("decimal", str(value))
    if isinstance(value, datetime):
        return ("datetime", value.isoformat())
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _matches(row: dict[str, Any], step: FilterStep) -> bool:
    try:
        actual = _resolve(row, step.field)
    except ComputeKernelError:
        if step.cmp == "empty":
            return True
        if step.cmp == "exists":
            return False
        raise
    if step.cmp == "exists":
        return not _is_empty(actual)
    if step.cmp == "empty":
        return _is_empty(actual)
    expected = step.value
    if step.cmp in {"in", "not_in"}:
        if not isinstance(expected, list):
            raise ComputeKernelError(f"{step.cmp} filter requires list value")
        values = [_typed(item, step.field.type) for item in expected]
        result = actual in values
        return result if step.cmp == "in" else not result
    expected = _typed(expected, step.field.type)
    if step.cmp in {"contains", "not_contains"}:
        result = str(expected).casefold() in str(actual).casefold()
        return result if step.cmp == "contains" else not result
    comparisons = {
        "eq": lambda: actual == expected,
        "ne": lambda: actual != expected,
        "lt": lambda: actual < expected,
        "lte": lambda: actual <= expected,
        "gt": lambda: actual > expected,
        "gte": lambda: actual >= expected,
    }
    return comparisons[step.cmp]()


def _sort(rows: list[dict[str, Any]], keys: list[SortKey]) -> list[dict[str, Any]]:
    result = list(rows)
    for key in reversed(keys):
        present: list[tuple[Any, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for row in result:
            try:
                value = _resolve(row, key.field)
            except ComputeKernelError:
                missing.append(row)
                continue
            if _is_empty(value):
                missing.append(row)
            else:
                present.append((value, row))
        present.sort(key=lambda item: item[0], reverse=key.direction == "desc")
        result = [row for _, row in present] + missing
    return result


def _aggregate(rows: list[dict[str, Any]], spec: AggregateSpec) -> Any:
    if spec.fn == "count" and spec.field is None:
        return len(rows)
    assert spec.field is not None
    values = [_resolve(row, spec.field) for row in rows]
    values = [value for value in values if not _is_empty(value)]
    if spec.fn == "count":
        return len(values)
    if not values:
        return None
    if spec.fn == "sum":
        return sum(values, Decimal(0))
    if spec.fn == "avg":
        return sum(values, Decimal(0)) / Decimal(len(values))
    if spec.fn == "min":
        return min(values)
    return max(values)


def _bucket(value: Any, step: DateBucketStep) -> str:
    parsed = _typed(value, "datetime")
    assert isinstance(parsed, datetime)
    if step.format == "month_name":
        if step.unit != "month":
            raise ComputeKernelError("month_name format requires month date bucket")
        return calendar.month_name[parsed.month]
    if step.unit == "day":
        return parsed.strftime("%Y-%m-%d")
    if step.unit == "month":
        return parsed.strftime("%Y-%m")
    return parsed.strftime("%Y")


def _split_text(value: Any, step: TextSplitStep) -> str:
    text = str(value or "")
    split = text.rsplit if step.direction == "right" else text.split
    parts = split(step.separator, step.maxsplit)
    try:
        return parts[step.index]
    except IndexError as exc:
        raise ComputeKernelError(
            f"text split index out of range: index={step.index}, parts={len(parts)}"
        ) from exc


def _arithmetic(value: Any, step: ArithmeticStep) -> Decimal:
    left = _resolve(value, step.field)
    if not isinstance(left, Decimal):
        left = _decimal(left)
    operand = _decimal(step.operand)
    if step.operator == "add":
        result = left + operand
    elif step.operator == "subtract":
        result = left - operand
    elif step.operator == "multiply":
        result = left * operand
    else:
        if operand == 0:
            raise ComputeKernelError("arithmetic divide operand cannot be zero")
        result = left / operand
    if step.round_digits is not None:
        quantum = Decimal(1).scaleb(-step.round_digits)
        result = result.quantize(quantum, rounding=ROUND_HALF_UP)
    return result


def execute_pipeline(source: Any, steps: list[ComputeStep]) -> tuple[Any, list[str]]:
    """Execute a bounded linear transformation and return its trace."""
    scalar_first = steps and isinstance(steps[0], (ArithmeticStep, BuildRecordStep))
    current: Any = source if scalar_first else _records(source)
    trace: list[str] = []
    for step in steps:
        if isinstance(step, BuildRecordStep):
            current = [{name: _resolve(current, ref) for name, ref in step.fields.items()}]
            trace.append("build_record:1->1")
            continue
        if isinstance(step, ArithmeticStep):
            if isinstance(current, list):
                rows = _records(current)
                if not step.output:
                    raise ComputeKernelError(
                        "arithmetic over record list requires output field"
                    )
                current = [
                    {**row, step.output: _arithmetic(row, step)}
                    for row in rows
                ]
                trace.append(f"arithmetic:{len(rows)}->{len(rows)}")
            elif step.output:
                result = _arithmetic(current, step)
                current = (
                    {**current, step.output: result}
                    if isinstance(current, dict)
                    else {step.output: result}
                )
                trace.append("arithmetic:1->1")
            else:
                current = _arithmetic(current, step)
                trace.append(f"arithmetic:{step.operator}")
            continue
        rows = _records(current)
        before = len(rows)
        if isinstance(step, FilterStep):
            current = [row for row in rows if _matches(row, step)]
        elif isinstance(step, SortStep):
            current = _sort(rows, step.keys)
        elif isinstance(step, TakeStep):
            current = rows[step.offset:step.offset + step.count]
        elif isinstance(step, ProjectStep):
            current = [
                {name: _resolve(row, ref) for name, ref in step.fields.items()}
                for row in rows
            ]
        elif isinstance(step, DistinctStep):
            seen: set[Any] = set()
            unique: list[dict[str, Any]] = []
            for row in rows:
                key = _fingerprint(
                    [_resolve(row, ref) for ref in step.fields]
                    if step.fields else row
                )
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
            current = unique
        elif isinstance(step, DateBucketStep):
            current = [
                {**row, step.output: _bucket(_resolve(row, step.field), step)}
                for row in rows
            ]
        elif isinstance(step, TextSplitStep):
            current = [
                {**row, step.output: _split_text(_resolve(row, step.field), step)}
                for row in rows
            ]
        elif isinstance(step, AggregateStep):
            current = {name: _aggregate(rows, spec) for name, spec in step.values.items()}
        elif isinstance(step, GroupStep):
            groups: dict[Any, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
            for row in rows:
                labels = {name: _resolve(row, ref) for name, ref in step.by.items()}
                key = _fingerprint(labels)
                groups.setdefault(key, (labels, []))[1].append(row)
            current = [
                {**labels, **{name: _aggregate(items, spec) for name, spec in step.values.items()}}
                for labels, items in groups.values()
            ]
        else:
            ranked = _sort(rows, step.keys)
            rank = 0
            previous: Any = object()
            selected: list[dict[str, Any]] = []
            for row in ranked:
                key = _fingerprint([_resolve(row, item.field) for item in step.keys])
                if key != previous:
                    rank += 1
                    previous = key
                if rank == step.position:
                    selected.append(row)
                elif rank > step.position:
                    break
            current = selected
        after = len(current) if isinstance(current, list) else 1
        trace.append(f"{step.op}:{before}->{after}")
    return current, trace
