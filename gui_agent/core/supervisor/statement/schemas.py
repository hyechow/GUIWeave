"""Schemas for the unified Statement Transition and runtime action capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
        description=(
            "source=journal 时必须是决策包中存在的 turn:N"
        ),
    )

    @model_validator(mode="after")
    def _validate_reference(self) -> "_TransitionEvidence":
        if self.source == "journal" and not self.event_ref.startswith("turn:"):
            raise ValueError(
                "journal transition evidence requires turn:N event_ref"
            )
        if self.source == "current_observation" and self.event_ref:
            raise ValueError("current observation evidence cannot carry event_ref")
        return self


class _TransitionAssessment(BaseModel):
    """Ephemeral Statement-state judgment produced before the next transition.

    This value is recorded for diagnostics but is never replayed as Runtime state.  Journal
    observations and receipts remain the only facts on the next frame.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["in_progress", "satisfied", "blocked"]
    summary: str = Field(min_length=1, description="当前 Statement 局势的一句话判断")
    established_facts: list[str] = Field(
        default_factory=list,
        description="当前观察或 Journal 已建立的相关事实，不得写未来动作",
    )
    open_gaps: list[str] = Field(
        default_factory=list,
        description="距离合同完成仍缺少的事实或边界；satisfied 时必须为空",
    )
    last_action_effect: Literal["effective", "no_effect", "unknown", "none"] = "none"

    @model_validator(mode="after")
    def _validate_gaps(self) -> "_TransitionAssessment":
        if self.status == "satisfied" and self.open_gaps:
            raise ValueError("satisfied assessment cannot carry open gaps")
        if self.status == "in_progress" and not self.open_gaps:
            raise ValueError("in_progress assessment requires at least one open gap")
        return self


class _TransitionAction(BaseModel):
    """One structured where+what action proposed by Transition."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(
        min_length=1,
        description=(
            "给视觉 Action Policy 的完整单步语义指令；必须说明在哪里对什么做什么，"
            "同名目标还要写出当前画面可见的区域或关系线索"
        ),
    )
    atomic_role: Literal["prepare", "write", "commit", "iterate"] = "prepare"
    action_family: Literal["input", "select", "activate", "navigate", "iterate"]
    target_control: str = Field(
        default="",
        description=(
            "动作所针对的当前控件或入口可读名称；具名 activate/navigate 以及 "
            "input/select 必填；target_ref 存在时由 ref 承担精确身份"
        ),
    )
    target_value: str = Field(
        default="",
        description="input/select 要写入或选择的合同精确值；其他动作通常留空",
    )
    target_ref: str = Field(
        default="",
        description="当前语义入口清单给出的精确 ref；没有 ref 的目标留空",
    )
    expected_result: str = Field(
        min_length=1,
        description="执行后下一帧应观察到的具体变化，用于下一次状态判断",
    )
    direction: Optional[
        Literal["up", "down", "left", "right", "increase", "decrease"]
    ] = None
    drag_column: Optional[str] = None
    drag_current_value: Optional[int] = None
    drag_target_value: Optional[int] = None

    @field_validator(
        "instruction", "target_control", "target_value", "target_ref", mode="before"
    )
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else value

    @model_validator(mode="after")
    def _require_structured_action(self) -> "_TransitionAction":
        if not self.target_control.strip():
            raise ValueError("transition action requires target_control")
        if self.action_family in {"input", "select"} and not self.target_value.strip():
            raise ValueError(f"{self.action_family} transition requires target_value")
        return self


class _StatementTransitionResult(BaseModel):
    """Minimal LLM decision for one Statement frame.

    Waiting is a Runtime concern (loading or a dispatched asynchronous operation), not a
    semantic decision. Recovery is simply another ``act`` with a different tactic. Runtime
    derives completion verification from evidence instead of asking the model to grade itself.
    """

    model_config = ConfigDict(extra="forbid")

    assessment: _TransitionAssessment
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
    @field_validator("summary", "page_identity", "kickback", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else value

    @model_validator(mode="after")
    def _validate_kind_payload(self) -> "_StatementTransitionResult":
        if self.kind == "act":
            if self.action is None:
                raise ValueError("act transition requires one action")
        elif self.action is not None:
            raise ValueError(f"{self.kind} transition cannot carry an action")
        if self.kind != "infeasible" and self.kickback:
            raise ValueError(f"{self.kind} transition cannot carry kickback")
        if self.kind in {"complete", "infeasible"} and not self.evidence:
            raise ValueError(f"{self.kind} transition requires cited evidence")
        if self.kind == "infeasible" and not self.kickback.strip():
            raise ValueError("infeasible transition requires kickback")
        expected_status = {
            "act": "in_progress",
            "complete": "satisfied",
            "infeasible": "blocked",
        }[self.kind]
        if self.assessment.status != expected_status:
            raise ValueError(
                f"{self.kind} transition requires assessment.status={expected_status}"
            )
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
    target_ref: str = ""
    expected_result: str = ""
    direction: Optional[
        Literal["up", "down", "left", "right", "increase", "decrease"]
    ] = None
    drag_column: Optional[str] = None
    drag_current_value: Optional[int] = None
    drag_target_value: Optional[int] = None

    @field_validator("target_control", "target_value", "target_ref", mode="before")
    @classmethod
    def _coerce_optional_string(cls, value):
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            return str(value)
        return value
