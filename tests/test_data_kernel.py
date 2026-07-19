from decimal import Decimal

import pytest

from gui_agent.core.run.statements.data_kernel import (
    AggregateSpec,
    AggregateStep,
    DataKernelError,
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
    describe_datasets,
    execute_pipeline,
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
    with pytest.raises(DataKernelError, match="field path does not exist"):
        execute_pipeline(
            [{"real": "value"}],
            [ProjectStep(fields={"x": FieldRef(path=["guessed"])})],
        )


def test_dataset_schema_exposes_exact_fields_types_and_coverage():
    schemas = describe_datasets([{
        "caption": "Orders",
        "rows": [{
            "Purchase Date": "May 19, 2023",
            "Grand Total": "$93.40",
            "ID": "42",
            "Enabled": "Yes",
        }],
        "total_records": 153,
        "partial": True,
    }])

    fields = {field["name"]: field["type"] for field in schemas[0]["fields"]}
    assert fields == {
        "Purchase Date": "datetime",
        "Grand Total": "money",
        "ID": "number",
        "Enabled": "boolean",
    }
    assert schemas[0]["partial"] is True
    assert schemas[0]["total_records"] == 153
