"""Schemas for the unified Statement Transition and runtime action capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gui_agent.core.schemas import ActionFamily, AtomicRole, StatementContract


def action_metadata(plan, statement: StatementContract) -> tuple[AtomicRole, ActionFamily]:
    """Normalize capability metadata against the statement execution strategy."""
    if statement.is_iterative:
        return "iterate", "iterate"
    return plan.atomic_role, plan.action_family


@dataclass(frozen=True)
class StatementPrompts:
    """Platform seam for the one live Statement Transition prompt."""

    # iPhone screenshots are Retina and are halved for vision prompts; browser/Android
    # observations already use their reasoning coordinate space.
    image_resize: Literal["retina", "none"] = "retina"
    home_identity_markers: tuple[str, ...] = ()
    # Empty loads the shared registry prompt. An adapter may override only this one decision.
    transition: str = ""

    @classmethod
    def neutral(cls) -> "StatementPrompts":
        return cls(
            image_resize="none",
            transition=(
                "Decide one kind: act|complete|infeasible from the contract, memory facts, "
                "and the current observation. Act requires exactly one action."
            ),
        )


class _TransitionEvidence(BaseModel):
    """One LLM-cited observation or an exact Journal event reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["current_observation", "journal"]
    claim: str = Field(min_length=1, description="该证据支持的可核验事实")
    event_ref: str = Field(
        default="",
        description="source=journal 时必须是 StatementMemory 中存在的 turn:N",
    )

    @model_validator(mode="after")
    def _validate_reference(self) -> "_TransitionEvidence":
        if self.source == "journal" and not self.event_ref.startswith("turn:"):
            raise ValueError("journal transition evidence requires turn:N event_ref")
        if self.source == "current_observation" and self.event_ref:
            raise ValueError("current observation evidence cannot carry event_ref")
        return self


def _normalize_atomic_role(value: object) -> object:
    """Tolerate an ``action_family`` value leaking into ``atomic_role`` (e.g. 'navigate').

    The two fields both label the action and are easily swapped; an action_family value here
    fails the primary ``json_object`` parse and triggers the slow plain-text reparse. Map the
    leak to the closest atomic_role; default to 'prepare'.
    """
    role = str(value or "").strip().lower()
    if role in {"prepare", "write", "commit", "iterate"}:
        return role
    return {
        "input": "write", "select": "write",
        "navigate": "prepare", "activate": "prepare", "unknown": "prepare",
        "iterate": "iterate",
    }.get(role, "prepare")


class _TransitionAction(BaseModel):
    """One semantic atomic action proposed by Transition."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, description="一个原子 GUI 动作")
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = "prepare"
    action_family: Literal[
        "input", "select", "activate", "navigate", "iterate", "unknown"
    ] = "unknown"
    target_control: str = Field(
        default="",
        description=(
            "动作所针对的当前可见控件或入口原名；具名 activate/navigate 以及 "
            "input/select 必填"
        ),
    )
    target_value: str = Field(
        default="",
        description="input/select 要写入或选择的合同精确值；其他动作通常留空",
    )
    direction: Optional[
        Literal["up", "down", "left", "right", "increase", "decrease"]
    ] = None
    drag_column: Optional[str] = None
    drag_current_value: Optional[int] = None
    drag_target_value: Optional[int] = None

    @field_validator(
        "instruction", "target_control", "target_value", mode="before"
    )
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else value

    @field_validator("target_control")
    @classmethod
    def _normalize_structured_target(cls, value: str) -> str:
        """Accept ``role: label`` as typed syntax, never as prose to interpret."""
        prefix, separator, label = value.partition(":")
        if (
            separator
            and prefix.strip().casefold()
            in {
                "button",
                "checkbox",
                "control",
                "entry",
                "field",
                "input",
                "link",
                "menuitem",
                "option",
                "radio",
                "select",
                "tab",
            }
            and label.strip()
        ):
            return label.strip()
        return value.strip()

    @field_validator("atomic_role", mode="before")
    @classmethod
    def _coerce_atomic_role(cls, value):
        return _normalize_atomic_role(value)

    @model_validator(mode="after")
    def _require_instruction(self) -> "_TransitionAction":
        if not self.instruction.strip():
            raise ValueError("transition action requires one instruction")
        return self


class _StatementTransitionResult(BaseModel):
    """Minimal LLM decision for one Statement frame.

    Waiting is a Runtime concern (loading or a dispatched asynchronous operation), not a
    semantic decision. Recovery is simply another ``act`` with a different tactic. Runtime
    derives completion verification from evidence instead of asking the model to grade itself.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["act", "complete", "infeasible"]
    # Emit the concrete payload before prose in the JSON schema. If a provider truncates a long
    # reason containing DOM quotes, an already-emitted action remains recoverable.
    action: Optional[_TransitionAction] = None
    reason: str = Field(min_length=1, description="决策理由")
    summary: str = Field(default="", description="当前屏幕/局势一句话摘要")
    evidence: list[_TransitionEvidence] = Field(default_factory=list)
    page_identity: str = Field(default="", description="页面身份描述")
    kickback: str = Field(
        default="",
        description="kind=infeasible 时给 ProgramRuntime 的重规划约束；其他 kind 留空",
    )
    read_instruction: Optional[str] = Field(
        default=None,
        description="collection/verification 当前帧的读取说明",
    )

    @field_validator("summary", "page_identity", "kickback", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else value

    @model_validator(mode="before")
    @classmethod
    def _normalize_redundant_payload(cls, value: object) -> object:
        """Normalize only contradictions whose typed discriminator has clear authority."""
        if not isinstance(value, dict):
            return value
        data = dict(value)
        kind = str(data.get("kind") or "").strip().lower()
        if kind in {"complete", "infeasible"}:
            data["action"] = None
        if kind == "act" and isinstance(data.get("action"), dict):
            if not str(data.get("reason") or "").strip():
                data["reason"] = (
                    str(data["action"].get("instruction") or "").strip()
                    or "execute the proposed action"
                )
        if kind == "act" and isinstance(data.get("evidence"), list):
            data["evidence"] = [
                item
                for item in data["evidence"]
                if not isinstance(item, dict)
                or item.get("source") != "journal"
                or str(item.get("event_ref") or "").startswith("turn:")
            ]
        return data

    @model_validator(mode="after")
    def _validate_kind_payload(self) -> "_StatementTransitionResult":
        if self.kind == "act":
            if self.action is None:
                raise ValueError("act transition requires one action")
        elif self.action is not None:
            raise ValueError(f"{self.kind} transition cannot carry an action")
        if self.kind != "infeasible" and self.kickback:
            raise ValueError(f"{self.kind} transition cannot carry kickback")
        return self


class _ActionDraft(BaseModel):
    """Mutable normalization draft before producing a frozen ActionIntent."""

    instruction: str = Field(description="下一步精确操作指令")
    summary: str = Field(description="规划依据一句话摘要")
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = "prepare"
    action_family: Literal[
        "input", "select", "activate", "navigate", "iterate", "unknown"
    ] = "unknown"
    target_control: str = ""
    target_value: str = ""
    direction: Optional[
        Literal["up", "down", "left", "right", "increase", "decrease"]
    ] = None
    drag_column: Optional[str] = None
    drag_current_value: Optional[int] = None
    drag_target_value: Optional[int] = None

    @field_validator("target_control", "target_value", mode="before")
    @classmethod
    def _coerce_optional_string(cls, value):
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        return value

    @field_validator("atomic_role", mode="before")
    @classmethod
    def _coerce_atomic_role(cls, value):
        return _normalize_atomic_role(value)
