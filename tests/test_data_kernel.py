from decimal import Decimal
from datetime import datetime

import pytest

from gui_agent.core.run.statements.compute_kernel import (
    AggregateSpec,
    AggregateStep,
    ArithmeticStep,
    BuildRecordStep,
    ComputeKernelError,
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
    execute_pipeline,
    normalize_table_rows,
)


def test_pipeline_builds_record_from_scalar():
    result, trace = execute_pipeline(
        "Fleece",
        [BuildRecordStep(fields={"Material": FieldRef(path=[])})],
    )

    assert result == [{"Material": "Fleece"}]
    assert trace == ["build_record:1->1"]


def test_pipeline_applies_rounded_scalar_arithmetic():
    result, trace = execute_pipeline(
        "$75.00",
        [ArithmeticStep(
            field=FieldRef(path=[], type="money"),
            operator="multiply",
            operand=0.865,
            round_digits=2,
        )],
    )

    assert result == Decimal("64.88")
    assert trace == ["arithmetic:multiply"]


def test_scalar_arithmetic_can_name_its_result():
    result, trace = execute_pipeline(
        "$75.00",
        [ArithmeticStep(
            field=FieldRef(path=[], type="money"),
            operator="multiply",
            operand=0.865,
            output="new_price",
            round_digits=2,
        )],
    )

    assert result == {"new_price": Decimal("64.88")}
    assert trace == ["arithmetic:1->1"]


def test_arithmetic_root_field_cannot_request_semantic_binding():
    step = ArithmeticStep(
        field=FieldRef(path=[], type="number", semantic=True),
        operator="add",
        operand=1,
    )

    assert step.field.semantic is False


def test_arithmetic_maps_record_list_and_preserves_identity():
    result, trace = execute_pipeline(
        [
            {"id": "a", "price": "$75.00"},
            {"id": "b", "price": "$100.00"},
        ],
        [ArithmeticStep(
            field=FieldRef(path=["price"], type="money"),
            operator="multiply",
            operand=0.865,
            output="new_price",
            round_digits=2,
        )],
    )

    assert result == [
        {"id": "a", "price": "$75.00", "new_price": Decimal("64.88")},
        {"id": "b", "price": "$100.00", "new_price": Decimal("86.50")},
    ]
    assert trace == ["arithmetic:2->2"]


def test_arithmetic_record_list_requires_output_field():
    with pytest.raises(ComputeKernelError, match="requires output field"):
        execute_pipeline(
            [{"price": 10}],
            [ArithmeticStep(
                field=FieldRef(path=["price"], type="number"),
                operator="add",
                operand=1,
            )],
        )


def test_pipeline_rejects_division_by_zero():
    with pytest.raises(ComputeKernelError, match="divide operand cannot be zero"):
        execute_pipeline(
            10,
            [ArithmeticStep(
                field=FieldRef(path=[], type="number"),
                operator="divide",
                operand=0,
            )],
        )


def test_pipeline_filters_sorts_takes_projects_and_deduplicates():
    rows = [
        {"term": "large", "uses": "19", "enabled": "Yes"},
        {"term": "small", "uses": "4", "enabled": "No"},
        {"term": "medium", "uses": "12", "enabled": "Yes"},
        {"term": "large", "uses": "19", "enabled": "Yes"},
    ]
    result, trace = execute_pipeline(rows, [
        FilterStep(
            field=FieldRef(path=["enabled"], type="boolean"),
            value=True,
        ),
        DistinctStep(fields=[FieldRef(path=["term"])]),
        SortStep(keys=[SortKey(
            field=FieldRef(path=["uses"], type="number"),
            direction="desc",
        )]),
        TakeStep(count=2),
        ProjectStep(fields={
            "name": FieldRef(path=["term"], type="text"),
            "count": FieldRef(path=["uses"], type="number"),
        }),
    ])

    assert result == [
        {"name": "large", "count": Decimal("19")},
        {"name": "medium", "count": Decimal("12")},
    ]
    assert trace[-1] == "project:2->2"


def test_pipeline_derives_related_identity_with_right_text_split():
    result, trace = execute_pipeline(
        [{"member_code": "account-east-user"}],
        [TextSplitStep(
            field=FieldRef(path=["member_code"], type="text"),
            output="account_code",
            separator="-",
            direction="right",
            maxsplit=1,
            index=0,
        )],
    )

    assert result == [{"member_code": "account-east-user", "account_code": "account-east"}]
    assert trace == ["text_split:1->1"]


def test_pipeline_replays_recent_completed_order_total_case():
    rows = [
        {"Purchase Date": "May 19, 2023", "Grand Total": "$93.40"},
        {"Purchase Date": "May 14, 2023", "Grand Total": "$89.00"},
        {"Purchase Date": "April 30, 2023", "Grand Total": "$100.00"},
    ]
    result, _ = execute_pipeline(rows, [
        SortStep(keys=[SortKey(
            field=FieldRef(path=["Purchase Date"], type="datetime"),
            direction="desc",
        )]),
        TakeStep(count=2),
        AggregateStep(values={
            "total": AggregateSpec(
                fn="sum",
                field=FieldRef(path=["Grand Total"], type="money"),
            ),
        }),
    ])

    assert result == {"total": Decimal("182.40")}


def test_table_boundary_normalizes_unambiguous_dates_and_money():
    rows = normalize_table_rows([{
        "Purchase Date": "Jun 9, 2023 9:00:00 AM",
        "Grand Total": "$1,234.50",
        "Uses": "19",
        "Rating": "4.5",
        "ID": "000062",
        "Status": "Complete",
    }])

    assert rows == [{
        "Purchase Date": "2023-06-09T09:00:00+00:00",
        "Grand Total": 1234.5,
        "Uses": 19,
        "Rating": 4.5,
        "ID": "000062",
        "Status": "Complete",
    }]


def test_table_boundary_honors_explicit_field_types():
    rows = normalize_table_rows(
        [{
            "Purchase Date": "Jun 9, 2023 9:00:00 AM",
            "Grand Total": "$1,234.50",
        }],
        {
            "Purchase Date": "datetime",
            "Grand Total": "number",
        },
    )

    assert rows[0]["Purchase Date"] == datetime.fromisoformat(
        "2023-06-09 09:00:00+00:00"
    )
    assert rows[0]["Grand Total"] == 1234.5


def test_pipeline_buckets_groups_and_dense_ranks_with_ties():
    rows = [
        {"date": "2023-05-02", "customer": "a"},
        {"date": "2023-05-14", "customer": "a"},
        {"date": "2023-04-03", "customer": "b"},
        {"date": "2023-04-09", "customer": "b"},
        {"date": "2023-03-01", "customer": "c"},
    ]
    result, _ = execute_pipeline(rows, [
        DateBucketStep(
            field=FieldRef(path=["date"], type="datetime"),
            output="month",
        ),
        GroupStep(
            by={"month": FieldRef(path=["month"])},
            values={"count": AggregateSpec(fn="count")},
        ),
        RankStep(
            keys=[SortKey(
                field=FieldRef(path=["count"], type="number"),
                direction="desc",
            )],
            position=1,
        ),
    ])

    assert result == [
        {"month": "2023-05", "count": 2},
        {"month": "2023-04", "count": 2},
    ]


def test_pipeline_reports_missing_fields_instead_of_guessing():
    with pytest.raises(ComputeKernelError, match="field path does not exist"):
        execute_pipeline(
            [{"real": "value"}],
            [ProjectStep(fields={"x": FieldRef(path=["guessed"])})],
        )
