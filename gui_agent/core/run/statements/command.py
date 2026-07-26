"""Deterministic platform Command executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from gui_agent.core.run.contracts import Command, StatementInvocation
from gui_agent.core.schemas import JsonValue, StatementOutcome

from .observation import ObservationCursor


def _method(client: Any, *names: str):
    for name in names:
        candidate = getattr(client, name, None)
        if callable(candidate):
            return candidate
    return None


def can_execute_command(invocation: StatementInvocation | None, platform: Any) -> bool:
    if invocation is None or not isinstance(invocation.statement, Command):
        return False
    client = getattr(platform, "client", None)
    capability = invocation.statement.capability
    if capability == "open_url":
        return _method(client, "navigate", "open_url") is not None
    if capability == "back":
        return _method(client, "go_back", "back") is not None
    return _method(client, "launch_app", "open_app") is not None


def execute_command(
    invocation: StatementInvocation,
    *,
    statement_index: int,
    cursor: ObservationCursor,
    platform: Any,
    status: Callable[[str], None],
    say: Callable[[str], None],
) -> StatementOutcome:
    statement = invocation.statement
    if not isinstance(statement, Command):
        raise TypeError("execute_command requires a Command invocation")
    client = getattr(platform, "client", None)
    try:
        if statement.capability == "open_url":
            url = str(invocation.args.get("url") or "")
            if not url:
                raise ValueError("open_url requires a non-empty url")
            action = _method(client, "navigate", "open_url")
            if action is None:
                raise NotImplementedError("platform does not support open_url")
            status(f"Command open_url: {url}")
            action(url)
        elif statement.capability == "back":
            action = _method(client, "go_back", "back")
            if action is None:
                raise NotImplementedError("platform does not support back")
            status("Command back")
            action()
        else:
            app = str(invocation.args.get("app") or "")
            if not app:
                raise ValueError("launch_app requires a non-empty app")
            action = _method(client, "launch_app", "open_app")
            if action is None:
                raise NotImplementedError("platform does not support launch_app")
            status(f"Command launch_app: {app}")
            action(app)
        settle = getattr(client, "wait_settled", None)
        if callable(settle):
            settle(statement.capability)
        observation = cursor.refresh(f"screenshot_command_{statement_index}.png")
        outputs: dict[str, JsonValue] = {}
        for name in statement.returns:
            if name == "url":
                outputs[name] = str(getattr(observation, "url", "") or "")
            elif name == "title":
                outputs[name] = str(getattr(observation, "title", "") or "")
        say(f"  [Program] Command {statement.capability} completed")
        return StatementOutcome.completed(
            f"Command {statement.capability}",
            outputs=outputs,
            observation=observation,
            observation_url=cursor.observation_url,
            evidence=[f"command:{statement.capability}"],
        )
    except Exception as exc:  # noqa: BLE001 - platform capability boundary
        return StatementOutcome.failed(
            f"Command {statement.capability} unavailable: {exc}",
            observation=cursor.observation,
            observation_url=cursor.observation_url,
            failure_evidence=str(exc),
        )


__all__ = ["can_execute_command", "execute_command"]
