import pytest

from gui_agent.core.orchestrator import (
    ObservationBinding,
    OutputSpec,
    Read,
    SourceCheck,
    ValueRef,
)
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.statements.binding import execute_read, execute_source_check
from gui_agent.core.schemas import Observation


def _read_invocation(returns, reads, *, inputs=None):
    return StatementInvocation(
        statement=Read(
            id="read",
            inputs={name: ValueRef(var=name) for name in (inputs or {})},
            reads=reads,
            returns=returns,
        ),
        inputs=inputs or {},
    )


def test_source_check_binds_exact_normalized_input_fields():
    invocation = StatementInvocation(
        statement=SourceCheck(
            id="check",
            required_fields=["purchase date", "status"],
        ),
        inputs={"records": [{"Purchase Date": "Jan 1, 2023", "Status": "Complete"}]},
    )

    outcome = execute_source_check(invocation, observation=None)

    assert outcome.is_completed
    assert outcome.verification == "confirmed"
    assert outcome.outputs == {
        "available": True,
        "bindings": {"purchase date": "Purchase Date", "status": "Status"},
        "missing_fields": [],
    }


def test_source_check_reports_missing_observation_fields():
    invocation = StatementInvocation(
        statement=SourceCheck(
            id="check",
            required_fields=["identity", "amount"],
        )
    )

    outcome = execute_source_check(
        invocation,
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{"headers": ["Identity"], "rows": []}],
        ),
    )

    assert outcome.outputs == {
        "available": False,
        "bindings": {"identity": "Identity"},
        "missing_fields": ["amount"],
    }


def test_read_rejects_typed_inputs():
    outcome = execute_read(
        _read_invocation(
            {"count": OutputSpec(type="number")},
            {"count": ObservationBinding(source="dataset", name="total_records")},
            inputs={"records": [{"id": 1}]},
        ),
        observation=None,
    )

    assert outcome.phase == "exhausted"
    assert "Compute" in outcome.summary


def test_read_binds_current_page_url():
    outcome = execute_read(
        _read_invocation(
            {"url": OutputSpec(type="url")},
            {"url": ObservationBinding(source="page", name="url")},
        ),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            url="https://example.test/current",
        ),
    )

    assert outcome.is_completed
    assert outcome.outputs == {"url": "https://example.test/current"}
    assert outcome.evidence == ["bind:page.url->url"]


def test_read_binds_declared_control_and_semantic_facts():
    outcome = execute_read(
        _read_invocation(
            {"filter": OutputSpec(), "heading": OutputSpec()},
            {
                "filter": ObservationBinding(source="field", name="Status"),
                "heading": ObservationBinding(source="field", name="heading"),
            },
        ),
        observation=Observation(
            png_bytes=b"png",
            source="android",
            form_controls=[{"label": "Status", "value": "Complete"}],
            semantic_tree=[{"role": "heading", "key": "Order details"}],
        ),
    )

    assert outcome.outputs == {"filter": "Complete", "heading": "Order details"}


def test_read_accepts_authoritative_empty_optional_control_value():
    outcome = execute_read(
        _read_invocation(
            {"value": OutputSpec(type="text", required=False)},
            {"value": ObservationBinding(source="field", name="Priority")},
        ),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            form_controls=[{
                "kind": "native_select",
                "label": "Priority",
                "selected_text": "",
            }],
        ),
    )

    assert outcome.is_completed
    assert outcome.outputs == {"value": ""}


def test_read_binds_primary_text_from_multiselect():
    outcome = execute_read(
        _read_invocation(
            {"value": OutputSpec(type="text")},
            {"value": ObservationBinding(source="field", name="Material")},
        ),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            form_controls=[{
                "kind": "native_select",
                "label": "Material",
                "selected_text": "Cotton, Lycra&reg;",
                "selected_text_primary": "Cotton",
            }],
        ),
    )

    assert outcome.outputs == {"value": "Cotton"}
    assert outcome.evidence == ["bind:controls[0].selected_text_primary->value"]
    assert outcome.verification == "confirmed"


def test_read_does_not_infer_empty_from_another_field():
    outcome = execute_read(
        _read_invocation(
            {"value": OutputSpec(type="text", required=False)},
            {"value": ObservationBinding(source="field", name="Priority")},
        ),
        observation=Observation(
            png_bytes=b"",
            source="browser",
            form_controls=[{"label": "Category", "selected_text": ""}],
        ),
    )

    assert outcome.phase == "infeasible"


def test_read_binds_structural_dataset_total():
    outcome = execute_read(
        _read_invocation(
            {"total_count": OutputSpec(type="number")},
            {
                "total_count": ObservationBinding(
                    source="dataset", name="total_records"
                )
            },
        ),
        observation=Observation(
            png_bytes=b"png",
            source="browser",
            tables=[{
                "rows": [{"ID": "199"}, {"ID": "73"}],
                "total_records": 2,
                "partial": False,
            }],
        ),
    )

    assert outcome.outputs == {"total_count": 2}


def test_read_uses_declared_field_for_direct_visual_extraction(monkeypatch):
    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.structured_read.structured_read",
        lambda *_args, **_kwargs: {"amount": "1,234"},
    )
    outcome = execute_read(
        _read_invocation(
            {"amount": OutputSpec(type="number", description="visible final amount")},
            {"amount": ObservationBinding(source="field", name="final amount")},
        ),
        observation=Observation(png_bytes=b"png", source="iphone"),
    )

    assert outcome.outputs == {"amount": 1234}
    assert outcome.verification == "accepted_unverified"


def test_read_visual_missing_fact_requests_program_correction(monkeypatch):
    monkeypatch.setattr(
        "gui_agent.core.orchestrator.primitives.structured_read.structured_read",
        lambda *_args, **_kwargs: {"amount": ""},
    )
    outcome = execute_read(
        _read_invocation(
            {"amount": OutputSpec(type="number")},
            {"amount": ObservationBinding(source="field", name="final amount")},
        ),
        observation=Observation(png_bytes=b"png", source="iphone"),
    )

    assert outcome.phase == "infeasible"
    assert "Interact" in (outcome.kickback or "")


def test_read_rejects_conflicting_values_for_one_declared_field():
    outcome = execute_read(
        _read_invocation(
            {"value": OutputSpec()},
            {"value": ObservationBinding(source="field", name="Status")},
        ),
        observation=Observation(
            png_bytes=b"",
            source="browser",
            form_controls=[
                {"label": "Status", "value": "Open"},
                {"label": "Status", "value": "Closed"},
            ],
        ),
    )

    assert outcome.phase == "infeasible"
