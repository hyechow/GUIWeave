"""Pure, typed DSL for deterministic record transformations."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


ValueType = Literal["auto", "text", "number", "money", "datetime", "boolean"]


class _KernelModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldRef(_KernelModel):
    path: list[str | int]
    type: ValueType = "auto"
    semantic: bool = False


class FilterStep(_KernelModel):
    op: Literal["filter"] = "filter"
    field: FieldRef
    cmp: Literal[
        "eq", "ne", "lt", "lte", "gt", "gte",
        "contains", "not_contains", "in", "not_in", "exists", "empty",
    ] = "eq"
    value: JsonValue = None

    @field_validator("cmp", mode="before")
    @classmethod
    def _normalize_not_empty(cls, value):
        return "exists" if value in {"not_empty", "nonempty", "is_not_empty"} else value


class SortKey(_KernelModel):
    field: FieldRef
    direction: Literal["asc", "desc"] = "asc"


class SortStep(_KernelModel):
    op: Literal["sort"] = "sort"
    keys: list[SortKey] = Field(min_length=1)


class TakeStep(_KernelModel):
    op: Literal["take"] = "take"
    count: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)


class ProjectStep(_KernelModel):
    op: Literal["project"] = "project"
    fields: dict[str, FieldRef] = Field(min_length=1)

    @field_validator("fields", mode="before")
    @classmethod
    def _normalize_field_wrappers(cls, value):
        if not isinstance(value, dict):
            return value
        return {
            name: item["field"]
            if isinstance(item, dict) and set(item) == {"field"}
            else item
            for name, item in value.items()
        }


class BuildRecordStep(_KernelModel):
    op: Literal["build_record"] = "build_record"
    fields: dict[str, FieldRef] = Field(min_length=1)


class DistinctStep(_KernelModel):
    op: Literal["distinct"] = "distinct"
    fields: list[FieldRef] = Field(default_factory=list)


class DateBucketStep(_KernelModel):
    op: Literal["date_bucket"] = "date_bucket"
    field: FieldRef
    output: str
    unit: Literal["day", "month", "year"] = "month"
    format: Literal["iso", "month_name"] = "iso"


class TextSplitStep(_KernelModel):
    op: Literal["text_split"] = "text_split"
    field: FieldRef
    output: str
    separator: str = Field(min_length=1)
    direction: Literal["left", "right"] = "left"
    maxsplit: int = Field(default=-1, ge=-1)
    index: int


class AggregateSpec(_KernelModel):
    fn: Literal["count", "sum", "min", "max", "avg"]
    field: FieldRef | None = None

    @model_validator(mode="after")
    def _field_required(self) -> "AggregateSpec":
        if self.fn != "count" and self.field is None:
            raise ValueError(f"{self.fn} aggregate requires field")
        return self


class AggregateStep(_KernelModel):
    op: Literal["aggregate"] = "aggregate"
    values: dict[str, AggregateSpec] = Field(min_length=1)


class GroupStep(_KernelModel):
    op: Literal["group"] = "group"
    by: dict[str, FieldRef] = Field(min_length=1)
    values: dict[str, AggregateSpec] = Field(min_length=1)


class RankStep(_KernelModel):
    op: Literal["rank"] = "rank"
    keys: list[SortKey] = Field(min_length=1)
    position: int = Field(ge=1)


ComputeStep = Annotated[
    Union[
        BuildRecordStep,
        FilterStep,
        SortStep,
        TakeStep,
        ProjectStep,
        DistinctStep,
        DateBucketStep,
        TextSplitStep,
        AggregateStep,
        GroupStep,
        RankStep,
    ],
    Field(discriminator="op"),
]


__all__ = [
    "AggregateSpec",
    "AggregateStep",
    "BuildRecordStep",
    "ComputeStep",
    "DateBucketStep",
    "DistinctStep",
    "FieldRef",
    "FilterStep",
    "GroupStep",
    "ProjectStep",
    "RankStep",
    "SortKey",
    "SortStep",
    "TakeStep",
    "TextSplitStep",
    "ValueType",
]
