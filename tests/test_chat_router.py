from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gui_agent.core.chat_router import ChatIntentRouter, ChatRoute


def test_chat_route_requires_payload_for_selected_route() -> None:
    with pytest.raises(ValidationError, match="gui route requires gui_goal"):
        ChatRoute(route="gui", reason="needs the current page")
    with pytest.raises(ValidationError, match="respond route requires reply"):
        ChatRoute(route="respond", reason="ordinary conversation")
    assert ChatRoute(route="cancel").cancel_task_id == ""
    assert ChatRoute(route="respond", reply="hello").reason == (
        "The request does not require current GUI access."
    )


def test_chat_router_supplies_bounded_history_and_platform() -> None:
    captured = {}

    def invoke(llm, messages, schema, **kwargs):
        captured.update(llm=llm, messages=messages, schema=schema, kwargs=kwargs)
        return ChatRoute(
            route="gui",
            gui_goal="Open the previously requested page again.",
            reason="follow-up repeats one exact GUI goal",
        )

    llm = SimpleNamespace()
    router = ChatIntentRouter(llm, invoke=invoke)
    history = [{"user": f"message {index}"} for index in range(20)]

    route = router.route("再试一次", history, "browser")

    payload = json.loads(captured["messages"][-1].content)
    assert route.route == "gui"
    assert captured["llm"] is llm
    assert captured["schema"] is ChatRoute
    assert captured["kwargs"]["trace_label"] == "chat.router"
    assert payload["platform"] == "browser"
    assert payload["message"] == "再试一次"
    assert payload["conversation"] == history[-12:]
    assert "Why did that fail?" in captured["messages"][0].content
    assert "identify yourself as GUIWeave" in captured["messages"][0].content
    assert "generic AI assistant" in captured["messages"][0].content
    assert "preface unrelated" in captured["messages"][0].content
    assert "Questions asking how to use an application are `respond`" in captured["messages"][0].content
    assert "Route a stop request to `cancel`" in captured["messages"][0].content
    assert "exact `task_id`" in captured["messages"][0].content
    assert "Details suggested earlier by the assistant" in captured["messages"][0].content


def test_chat_router_removes_urls_not_grounded_in_conversation() -> None:
    def invoke(_llm, _messages, _schema, **_kwargs):
        return ChatRoute(
            route="gui",
            gui_goal="打开 Google 首页 (https://www.google.com)",
            reason="the browser must navigate",
        )

    router = ChatIntentRouter(SimpleNamespace(), invoke=invoke)

    inferred = router.route("打开 Google", [], "browser")
    supplied = router.route("打开 https://www.google.com", [], "browser")
    assistant_only = router.route(
        "打开刚才的地址",
        [{"user": "推荐一个搜索引擎", "assistant": "https://www.google.com"}],
        "browser",
    )
    prior_user = router.route(
        "打开刚才的地址",
        [{"user": "地址是 https://www.google.com", "assistant": "收到"}],
        "browser",
    )
    bare_domain_router = ChatIntentRouter(
        SimpleNamespace(),
        invoke=lambda *_args, **_kwargs: ChatRoute(
            route="gui",
            gui_goal="Navigate to www.google.com",
            reason="the browser must navigate",
        ),
    )
    bare_domain = bare_domain_router.route(
        "Google",
        [{
            "user": "打开一个网站",
            "assistant": "请问哪个网站？",
            "route": "clarify",
        }],
        "browser",
    )

    assert inferred.gui_goal == "打开 Google"
    assert supplied.gui_goal == "打开 Google 首页 (https://www.google.com)"
    assert assistant_only.gui_goal == "打开刚才的地址"
    assert prior_user.gui_goal == "打开 Google 首页 (https://www.google.com)"
    assert bare_domain.gui_goal == "打开一个网站；Google"


def test_chat_router_keeps_chinese_turn_details_in_chinese() -> None:
    router = ChatIntentRouter(
        SimpleNamespace(),
        invoke=lambda *_args, **_kwargs: ChatRoute(
            route="gui",
            gui_goal="Navigate to Baidu's homepage.",
            reason="The request needs browser navigation.",
        ),
    )

    route = router.route(
        "百度",
        [{
            "user": "帮我打开一个网站",
            "assistant": "请问哪个网站？",
            "route": "clarify",
        }],
        "browser",
    )

    assert route.gui_goal == "帮我打开一个网站；百度"
    assert route.reason == "该请求需要读取或操作当前界面。"
