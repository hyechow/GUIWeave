"""Data contracts for the sole coding orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


FIELD_VALUE_TYPES = frozenset(
    {"auto", "text", "number", "money", "datetime", "boolean"}
)


def field_projection(
    value: list[str] | dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Normalize the public field projection and its optional value schema."""
    if not isinstance(value, (list, dict)):
        raise TypeError("fields must be a list of names or a name-to-type mapping")
    names = list(value)
    field_types = dict(value) if isinstance(value, dict) else {}
    if (
        not names
        or any(not isinstance(name, str) or not name.strip() for name in names)
        or any(str(value) not in FIELD_VALUE_TYPES for value in field_types.values())
    ):
        raise ValueError(
            f"fields require nonempty names and types from {sorted(FIELD_VALUE_TYPES)!r}"
        )
    return list(names), field_types


@dataclass(frozen=True)
class CodeDiagnostic:
    code: str
    message: str
    line: int = 0
    column: int = 0

    def render(self) -> str:
        where = f"line {self.line}:{self.column}" if self.line else "source"
        return f"[{self.code}] {where}: {self.message}"


@dataclass(frozen=True)
class TraceEvent:
    op: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    result: Any = None


@dataclass(frozen=True)
class UIStateHandle:
    """Runtime-issued capability and the verified state it represents."""

    token: str
    postcondition: dict[str, Any] = field(default_factory=dict)
    observed_state: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "postcondition": self.postcondition,
            "observed_state": self.observed_state,
        }


def collection_postcondition(value: Any) -> dict[str, Any] | None:
    """Validate the one public ``ctx.reach`` success shape."""
    if (
        not isinstance(value, dict)
        or "entity" not in value
        or not set(value) <= {"entity", "fields"}
    ):
        return None
    entity, fields = value["entity"], value.get("fields", [])
    if (
        not isinstance(entity, str)
        or not entity.strip()
        or not isinstance(fields, list)
        or any(not isinstance(item, str) or not item.strip() for item in fields)
    ):
        return None
    return {"entity": entity, "fields": list(fields)}


def require_ui_state(
    value: Any,
    *,
    entity: str = "",
    fields: list[str] | None = None,
) -> UIStateHandle:
    if not isinstance(value, UIStateHandle):
        raise ValueError(
            "ctx.query/ctx.read require the UIStateHandle returned by ctx.reach"
        )
    postcondition = value.postcondition
    if entity and postcondition.get("entity") != entity:
        raise ValueError("ctx.reach collection state does not satisfy ctx.query")
    return value


@dataclass(frozen=True)
class WriteEvent:
    goal: str
    success: str
    target_id: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    required_values: dict[str, Any] = field(default_factory=dict)
    observe_fields: list[str] = field(default_factory=list)
    persistence: str = "explicit_commit"
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    applied: bool = False


@dataclass
class CodingRunResult:
    ok: bool
    return_value: Any = None
    trace: list[TraceEvent] = field(default_factory=list)
    writes: list[WriteEvent] = field(default_factory=list)
    final_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ""
    timed_out: bool = False


@dataclass
class CodingAttempt:
    source: str
    diagnostics: list[CodeDiagnostic] = field(default_factory=list)
    run: CodingRunResult | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0


@dataclass(frozen=True)
class CodingReview:
    text: str
    approved: bool
    issues: tuple[CodeDiagnostic, ...] = ()
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    @property
    def unavailable(self) -> bool:
        return self.error.startswith(("[REVIEW_IO]", "[REVIEW_PROTOCOL]"))


@dataclass(frozen=True)
class CodingEvent:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.data}


@dataclass
class CodingPlan:
    goal: str
    source: str
    attempts: list[CodingAttempt] = field(default_factory=list)
    review: CodingReview | None = None
    events: list[CodingEvent] = field(default_factory=list)

    @property
    def executable(self) -> bool:
        return bool(
            self.attempts
            and not (attempt := self.attempts[-1]).diagnostics
            and (attempt.run is None or attempt.run.ok)
        )

    @property
    def repaired(self) -> bool:
        return len(self.attempts) > 1

    @property
    def requirements_satisfied(self) -> bool:
        # Reviewer output is audit evidence, never an execution gate.
        return self.executable
