"""Policy implementations for single-turn experiments."""

__all__ = ["StructuredOutputPolicy"]


def __getattr__(name: str):
    # Lazy so importing the leaf submodule ``policy_expr.core.policies.base`` (used by
    # core orchestration and the reader) does NOT eagerly drag in the iphone
    # adapter. ``policy_expr.core.policies.StructuredOutputPolicy`` still resolves.
    if name == "StructuredOutputPolicy":
        from policy_expr.adapters.iphone.policies.structured_output import (
            StructuredOutputPolicy,
        )

        return StructuredOutputPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
