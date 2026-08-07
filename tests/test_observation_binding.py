import pytest

from gui_agent.core.run.contracts import (
    ObservationBinding,
    OutputSpec,
    Read,
    StatementInvocation,
)
from gui_agent.core.run.statements.binding import execute_read
from gui_agent.core.schemas import Observation


def _read_invocation(returns, reads, *, inputs=None, args=None):
    return StatementInvocation(
        statement=Read(
            id="read",
            reads=reads,
            returns=returns,
        ),
        inputs=inputs or {},
        args=args or {},
    )


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
    assert "Python" in outcome.summary


def test_read_accepts_ui_state_dependency() -> None:
    outcome = execute_read(
        _read_invocation(
            {"url": OutputSpec(type="url")},
            {"url": ObservationBinding(source="page", name="url")},
            inputs={"ui_state": {
                "token": "state:1",
                "postcondition": {"kind": "target_fields_available"},
                "observed_state": {},
            }},
        ),
        observation=Observation(
            png_bytes=b"",
            source="browser",
            url="https://example.test/detail",
        ),
    )

    assert outcome.is_completed
    assert outcome.outputs == {"url": "https://example.test/detail"}


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


def test_read_does_not_bind_semantic_identity_as_its_own_value(monkeypatch):
    seen: list[bytes] = []
    monkeypatch.setattr(
        "gui_agent.core.run.structured_read.structured_read",
        lambda png, *_args, **_kwargs: (
            seen.append(png) or {"content": "first line\nsecond line"}
        ),
    )
    outcome = execute_read(
        _read_invocation(
            {"content": OutputSpec(type="text")},
            {"content": ObservationBinding(source="field", name="content")},
        ),
        observation=Observation(
            png_bytes=b"png",
            source="android",
            semantic_tree=[{"role": "group", "key": "content"}],
        ),
    )

    assert outcome.outputs == {"content": "first line\nsecond line"}
    assert seen == [b"png"]
    assert outcome.evidence == ["bind:visual.content->content"]
    assert outcome.verification == "accepted_unverified"


def test_read_binds_one_text_descendant_of_a_named_semantic_field(monkeypatch):
    content = "\n".join(["File content"] * 10)
    monkeypatch.setattr(
        "gui_agent.core.run.structured_read.structured_read",
        lambda *_args, **_kwargs: pytest.fail("structured text should win over vision"),
    )

    outcome = execute_read(
        _read_invocation(
            {"content": OutputSpec(type="text")},
            {"content": ObservationBinding(source="field", name="content")},
        ),
        observation=Observation(
            png_bytes=b"png",
            source="android",
            semantic_tree=[
                {"role": "group", "key": "content", "ref": "android:0.1"},
                {"role": "text", "key": content, "ref": "android:0.1.0"},
                {"role": "text", "key": "screen title", "ref": "android:0.0"},
            ],
        ),
    )

    assert outcome.outputs == {"content": content}
    assert outcome.evidence == ["bind:semantic[1].key->content"]
    assert outcome.verification == "confirmed"


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

    assert outcome.phase == "failed"


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
        "gui_agent.core.run.structured_read.structured_read",
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


def test_read_normalizes_single_numeric_value_with_display_unit(monkeypatch):
    monkeypatch.setattr(
        "gui_agent.core.run.structured_read.structured_read",
        lambda *_args, **_kwargs: {"Detailed Rating": "5 stars"},
    )
    outcome = execute_read(
        _read_invocation(
            {"Detailed Rating": OutputSpec(type="json")},
            {
                "Detailed Rating": ObservationBinding(
                    source="field",
                    name="Detailed Rating",
                )
            },
        ),
        observation=Observation(png_bytes=b"png", source="browser"),
    )

    assert outcome.outputs == {"Detailed Rating": 5}


def test_read_visual_missing_fact_requests_program_correction(monkeypatch):
    monkeypatch.setattr(
        "gui_agent.core.run.structured_read.structured_read",
        lambda *_args, **_kwargs: {"amount": ""},
    )
    outcome = execute_read(
        _read_invocation(
            {"amount": OutputSpec(type="number")},
            {"amount": ObservationBinding(source="field", name="final amount")},
        ),
        observation=Observation(png_bytes=b"png", source="iphone"),
    )

    assert outcome.phase == "failed"


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

    assert outcome.phase == "failed"


def test_read_generates_runtime_read_spec_and_passes_to_extraction(monkeypatch):
    """Lock the lunch read-pipeline fix: the read operation derives its own
    read_spec at execution time (one inference over fields/types/goal) and hands it
    to the visual extraction — the planner must not embed read_spec in the program."""
    captured: dict = {}
    spec = "body: 消息正文\nstart_ts: 按当前日期解析\nend_ts: 按当前日期解析"
    monkeypatch.setattr(
        "gui_agent.core.run.statements.binding._generate_read_spec",
        lambda fields, field_types, goal, check_knowledge: (
            captured.update(fields=list(fields), field_types=dict(field_types), goal=goal)
            or spec
        ),
    )
    monkeypatch.setattr(
        "gui_agent.core.run.structured_read.structured_read",
        lambda png, *args, **kwargs: (
            captured.update(read_spec=kwargs.get("read_spec"))
            or {"body": "hello", "start_ts": "2025-10-17 11:00"}
        ),
    )
    outcome = execute_read(
        _read_invocation(
            {"body": OutputSpec(type="json"), "start_ts": OutputSpec(type="json")},
            {
                "body": ObservationBinding(source="field", name="body"),
                "start_ts": ObservationBinding(source="field", name="start_ts"),
            },
            args={"field_types": {"body": "text", "start_ts": "datetime"}},
        ),
        observation=Observation(png_bytes=b"png", source="android"),
    )

    assert outcome.is_completed
    assert captured["fields"] == ["body", "start_ts"]
    assert captured["field_types"] == {"body": "text", "start_ts": "datetime"}
    assert captured["read_spec"] == spec


def test_generate_read_spec_empty_fields_returns_empty():
    from gui_agent.core.run.statements.binding import _generate_read_spec

    assert _generate_read_spec([], {}, "some goal") == ""


def test_generate_read_spec_prompt_forbids_hardcoded_dates(monkeypatch):
    """The read_spec generation must instruct the extractor to resolve relative
    dates against the current date, never hardcode a date into the spec — the
    planner that hardcoded the host clock was the original bug."""
    import gui_agent.core.run.statements.binding as binding

    seen: dict = {}

    class _FakeLLM:
        def __init__(self, **kwargs):
            pass

        def invoke(self, messages):
            seen["system"] = messages[0].content
            seen["human"] = messages[1].content
            return type("R", (), {"content": "start_ts: 按当前日期解析"})()

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeLLM)
    monkeypatch.setattr(
        "gui_agent.core.config.resolve_llm_config",
        lambda _name: type("C", (), {"model": "m", "api_key": "k", "base_url": "b"})(),
    )
    monkeypatch.setattr(
        "llm.provider_config.dashscope_extra_body", lambda _model: {}
    )

    spec = binding._generate_read_spec(
        ["body", "start_ts"],
        {"body": "text", "start_ts": "datetime"},
        "schedule lunch from the message",
        check_knowledge="calendar signal hints",
    )

    assert "不要硬编码具体日期" in seen["system"]
    assert "按当前日期解析" in spec
    assert "lunch from the message" in seen["human"]
    assert "calendar signal hints" in seen["human"]
