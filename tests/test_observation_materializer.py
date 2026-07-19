from gui_agent.core.run.observation_materializer import (
    materialize_observation,
    visual_dataset,
)
from gui_agent.core.schemas import Observation


def test_materializer_unifies_page_controls_semantics_and_tables():
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        url="https://example.test/orders",
        title="Orders",
        form_controls=[{"label": "Status", "value": "Complete"}],
        semantic_tree=[{"role": "heading", "key": "Orders", "ref": 7}],
        tables=[{
            "path": "#orders",
            "caption": "Orders",
            "headers": ["ID", "Total"],
            "rows": [{"ID": "1", "Total": "9"}],
            "total_records": 1,
        }],
        applied_filters={"Status": "Complete"},
    )

    normalized = materialize_observation(observation)

    assert normalized.page["url"] == "https://example.test/orders"
    assert normalized.controls[0]["value"] == "Complete"
    assert normalized.semantic[0]["key"] == "Orders"
    assert normalized.datasets[0].records == [{"ID": "1", "Total": "9"}]
    assert normalized.datasets[0].reliable is True
    assert normalized.applied_filters == {"Status": "Complete"}


def test_materializer_context_samples_records_but_kernel_transport_keeps_all():
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        tables=[{"headers": ["ID"], "rows": [{"ID": str(i)} for i in range(10)]}],
    )
    dataset = materialize_observation(observation).datasets[0]

    assert len(dataset.as_context()["sample_records"]) == 3
    assert "records" not in dataset.as_context()
    assert len(dataset.as_table()["rows"]) == 10


def test_visual_dataset_is_platform_neutral_and_marks_provenance_incomplete():
    observation = Observation(png_bytes=b"png", source="iphone")
    dataset = visual_dataset(
        observation,
        fields=["name"],
        records=[{"name": "A"}],
    )

    assert dataset.source == "visual"
    assert dataset.region == "main"
    assert dataset.records == [{"name": "A"}]
    assert dataset.provenance_incomplete is True
    assert dataset.reliable is False
