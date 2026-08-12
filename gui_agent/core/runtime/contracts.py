"""Small structural contracts at Tool Agent's platform boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from gui_agent.core.schemas import BaseAction, Observation
from gui_agent.core.runtime.clock import PlatformTimeSnapshot


@runtime_checkable
class PerceptionSession(Protocol):
    client: object

    def screenshot(self) -> bytes: ...

    def platform_time(self) -> PlatformTimeSnapshot: ...

    def __enter__(self) -> "PerceptionSession": ...

    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class Perception(Protocol):
    def observe(self) -> Observation: ...


@runtime_checkable
class ActionVisualizer(Protocol):
    def show_action(self, action: BaseAction) -> None: ...

    def clear(self) -> None: ...


__all__ = ["ActionVisualizer", "Perception", "PerceptionSession"]
