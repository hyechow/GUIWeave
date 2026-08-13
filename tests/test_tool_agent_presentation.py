from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.adapters.browser.webarena import (
    _synthesize_response,
)
from gui_agent.core.tool_agent.presentation import (
    present_result,
    result_digest,
    write_presentation_artifact,
)
from gui_agent.core.tool_agent.result import project_tool_agent_result


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

    assert presentation.status == "generated", presentation.error
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
    assert presentation.reply == "Result: a@example.com, b@example.com."
    assert "omitted canonical result" in presentation.error


def test_presenter_replaces_serialized_json_with_natural_language_fallback() -> None:
    value = {
        "date": "13日（明天）",
        "weather_condition": "多云转雷阵雨",
        "high_temperature": "33℃",
        "low_temperature": "26℃",
    }

    def invoke(_llm, _messages, schema, **_kwargs):
        return schema(
            reply=json.dumps(value, ensure_ascii=False),
            result_digest=result_digest(value),
        )

    presentation = present_result(
        goal="查一下明天的天气",
        phase="completed",
        result=value,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        invoke=invoke,
    )

    assert presentation.status == "fallback"
    assert presentation.reply.startswith("查询结果：")
    assert "13日（明天）" in presentation.reply
    assert "多云转雷阵雨" in presentation.reply
    assert not presentation.reply.startswith("{")
    assert "serialized structured data" in presentation.error


def test_presenter_translation_failure_falls_back_to_record_table() -> None:
    value = [
        {"order_id": "32", "status": "Completed"},
        {"order_id": "31", "status": "Canceled"},
    ]

    presentation = present_result(
        goal="查看订单列表",
        phase="completed",
        result=value,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        invoke=lambda _llm, _messages, schema, **_kwargs: schema(
            reply="订单 32 为 Completed，订单 31 为 Canceled。",
            result_digest=result_digest(value),
        ),
    )

    assert presentation.status == "fallback"
    assert presentation.reply == (
        "查询到 2 条结果：\n\n| order id | status |\n| --- | --- |\n"
        "| 32 | Completed |\n| 31 | Canceled |"
    )


def test_presenter_accepts_natural_reordering_and_verbalized_comparison() -> None:
    value = {
        "date": "13日（明天）",
        "weather": "多云转雷阵雨",
        "high_temperature": "33℃",
        "low_temperature": "26℃",
        "wind": "<3级",
    }

    def invoke(_llm, _messages, schema, **_kwargs):
        return schema(
            reply="明天（13日）天气为多云转雷阵雨，气温在26℃到33℃之间，风力小于3级。",
            result_digest=result_digest(value),
        )

    presentation = present_result(
        goal="查一下明天的天气",
        phase="completed",
        result=value,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        invoke=invoke,
    )

    assert presentation.status == "generated"
    assert presentation.reply.startswith("明天（13日）")
    assert presentation.error == ""


@pytest.mark.parametrize(
    ("result", "reply"),
    [
        ({"temperature": 3}, "温度为33℃。"),
        ({"wind": "<3级"}, "风力小于13级。"),
        ({"count": 20}, "共有120条。"),
    ],
)
def test_presenter_rejects_numeric_substrings_as_canonical_values(
    result: dict[str, object],
    reply: str,
) -> None:
    def invoke(_llm, _messages, schema, **_kwargs):
        return schema(
            reply=reply,
            result_digest=result_digest(result),
        )

    presentation = present_result(
        goal="请返回准确结果",
        phase="completed",
        result=result,
        summary="computed",
        replay={"status": "passed"},
        llm=object(),
        invoke=invoke,
    )

    assert presentation.status == "fallback"
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
    assert presentation.reply == "Result: answer is 42."
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
        platform_time={
            "platform": "browser",
            "local_datetime": "2026-08-12T19:10:27+08:00",
            "timezone": "Asia/Shanghai",
            "utc_offset": "+08:00",
            "source": "browser_cdp",
            "confidence": "authoritative",
            "captured_at": "2026-08-12T11:10:27.000+00:00",
            "fallback_reason": "",
        },
    )

    result = project_tool_agent_result(
        intent="Return the matching email",
        run=run,
        log_dir=tmp_path,
        knowledge_summary=None,
        platform="browser",
        fallback_task_type="RETRIEVE",
        presentation=presentation,
        app_router={
            "kind": "deterministic_app_router",
            "targets": [{"app_id": "RoboTeam"}],
            "active_app": "RoboTeam",
        },
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
    assert context["platform_time"]["source"] == "browser_cdp"
    assert result.orchestrator["platform_time"]["timezone"] == "Asia/Shanghai"
    assert context["orchestrator"]["app_router"]["active_app"] == "RoboTeam"

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

    result = project_tool_agent_result(
        intent=intent,
        run=run,
        log_dir=tmp_path,
        knowledge_summary=None,
        platform="browser",
        fallback_task_type=expected_type,
    )
    response = _synthesize_response(intent, result, tmp_path / "context.json")

    assert result.task_type == expected_type
    assert result.orchestrator["effect"] == effect
    assert response.task_type == expected_type
