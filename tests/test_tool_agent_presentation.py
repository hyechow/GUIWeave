from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.adapters.browser.webarena import (
    _synthesize_response,
    _tool_agent_result_and_context,
)
from gui_agent.core.tool_agent.presentation import (
    present_result,
    result_digest,
    write_presentation_artifact,
)


def test_presenter_turns_verified_result_into_natural_reply_without_capabilities() -> None:
    value = ["a@example.com", "b@example.com"]
    captured = {}

    def invoke(llm, messages, schema, **kwargs):
        captured["llm"] = llm
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return schema(
            reply=(
                "The customers with the requested order count are "
                "a@example.com and b@example.com."
            ),
            result_digest=result_digest(value),
        )

    presentation = present_result(
        goal="Return the matching customer emails",
        phase="completed",
        result=value,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        model_name="presenter-model",
        invoke=invoke,
    )

    assert presentation.status == "generated"
    assert presentation.model == "presenter-model"
    assert "a@example.com" in presentation.reply
    assert "b@example.com" in presentation.reply
    prompt = "\n".join(str(message.content) for message in captured["messages"])
    assert "Return the matching customer emails" in prompt
    assert '"result"' in prompt
    assert "browser" not in prompt.lower()
    assert "data_store" not in prompt


def test_presenter_falls_back_when_natural_reply_omits_a_result_literal() -> None:
    value = ["a@example.com", "b@example.com"]

    def invoke(_llm, _messages, schema, **_kwargs):
        return schema(
            reply="The matching customer is a@example.com.",
            result_digest=result_digest(value),
        )

    presentation = present_result(
        goal="Return the matching customer emails",
        phase="completed",
        result=value,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        invoke=invoke,
    )

    assert presentation.status == "fallback"
    assert json.loads(presentation.reply) == value
    assert "omitted canonical result" in presentation.error


def test_completed_result_is_not_sent_to_llm_until_replay_passes() -> None:
    called = False

    def invoke(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("Presenter must not run")

    presentation = present_result(
        goal="Return the result",
        phase="completed",
        result={"answer": 42},
        summary="computed",
        replay={"status": "failed"},
        llm=object(),
        invoke=invoke,
    )

    assert called is False
    assert presentation.status == "fallback"
    assert json.loads(presentation.reply) == {"answer": 42}
    assert "replay status" in presentation.error


def test_presentation_artifact_and_context_keep_result_and_reply_separate(tmp_path) -> None:
    value = ["a@example.com"]

    def invoke(_llm, _messages, schema, **_kwargs):
        return schema(
            reply="The matching email is a@example.com.",
            result_digest=result_digest(value),
        )

    presentation = present_result(
        goal="Return the matching email",
        phase="completed",
        result=value,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        model_name="presenter-model",
        invoke=invoke,
    )
    write_presentation_artifact(tmp_path, presentation)
    run = SimpleNamespace(
        phase="completed",
        effect="data",
        output=value,
        summary="computed",
        result_ref=None,
        perception_mode="enhanced",
        master_model="master",
        worker_model="worker",
        perception_model="perception",
    )

    result = _tool_agent_result_and_context(
        intent="Return the matching email",
        run=run,
        log_dir=tmp_path,
        knowledge_summary=None,
        presentation=presentation,
    )

    context = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    artifact = json.loads(
        (tmp_path / "tool_agent_presentation.json").read_text(encoding="utf-8")
    )
    assert json.loads(result.output) == value
    assert json.loads(context["outcome"]["output"]) == value
    assert context["reply"] == "The matching email is a@example.com."
    assert artifact["reply"] == context["reply"]
    assert context["models"]["tool_agent.presentation"] == "presenter-model"

    response = _synthesize_response(
        "Return the matching email",
        result,
        tmp_path / "context.json",
    )
    assert response.retrieved_data == value


@pytest.mark.parametrize(
    ("task_id", "intent", "effect", "expected_type"),
    [
        (
            709,
            "Show the orders report from May 1, 2021 to March 31, 2022.",
            "ui_state",
            "NAVIGATE",
        ),
        (
            488,
            'Change the page title of "Home Page".',
            "mutation",
            "MUTATE",
        ),
        (
            701,
            "Create a new marketing price rule.",
            "mutation",
            "MUTATE",
        ),
    ],
)
def test_live_task_effect_replay_preserves_original_task_semantics(
    tmp_path,
    task_id: int,
    intent: str,
    effect: str,
    expected_type: str,
) -> None:
    run = SimpleNamespace(
        phase="completed",
        effect=effect,
        output=True,
        summary=f"task {task_id} completed",
        result_ref=None,
        perception_mode="enhanced",
        master_model="master",
        worker_model="worker",
        perception_model="perception",
    )

    result = _tool_agent_result_and_context(
        intent=intent,
        run=run,
        log_dir=tmp_path,
        knowledge_summary=None,
    )
    response = _synthesize_response(intent, result, tmp_path / "context.json")

    assert result.task_type == expected_type
    assert result.orchestrator["effect"] == effect
    assert response.task_type == expected_type
