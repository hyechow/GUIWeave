"""Policy implementations for single-turn experiments."""

__all__ = ["StructuredOutputPolicy"]


def __getattr__(name: str):
    # Lazy so importing the leaf submodule ``gui_agent.core.policies.base`` (used by
    # core orchestration and the reader) does NOT eagerly drag in the iphone
    # adapter. ``gui_agent.core.policies.StructuredOutputPolicy`` still resolves.
    if name == "StructuredOutputPolicy":
        from gui_agent.adapters.iphone.policies.structured_output import (
            StructuredOutputPolicy,
        )

        return StructuredOutputPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
