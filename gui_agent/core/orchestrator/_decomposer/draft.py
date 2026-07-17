"""LLM-facing draft schema for the six-node semantic Program IR."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

from ..program import (
    Command,
    Condition,
    Data,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    Stmt,
    ValueRef,
    assign_statement_ids,
)


class _StepDraft(BaseModel):
    """Flat structured-output shape; ``op`` selects the relevant fields."""

    op: Literal["interact", "data", "command", "if", "foreach", "finish"] = Field(
        default="interact",
        description='"interact" | "data" | "command" | "if" | "foreach" | "finish"',
    )
    id: str = ""
    bind: str = ""
    goal: str = Field(
        default="",
        description=(
            "interact/data 的单一语义后置条件；不写页面路径、控件、SQL、表达式或候选分支"
        ),
    )
    success: str = Field(
        default="",
        description="interact 的业务验收条件；描述最终事实，不写中间按钮或页面相位",
    )
    inputs: dict[str, ValueRef] = Field(
        default_factory=dict,
        description="该 statement 可读取的上游 typed variables",
    )
    required_values: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="interact 不可改写的目标值、范围和实体事实",
    )
    scope: str = Field(default="", description="interact 的业务对象/范围说明")
    persistence: Literal["immediate", "explicit_commit"] = Field(
        default="immediate",
        description="interact: immediate | explicit_commit",
    )
    returns: dict[str, OutputSpec] = Field(
        default_factory=dict,
        description="typed output contract：字段名 → type/required/description",
    )
    capability: Literal["", "open_url", "back", "launch_app"] = Field(
        default="",
        description="command: open_url | back | launch_app",
    )
    args: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="command 的 literal 参数",
    )
    arg_refs: dict[str, ValueRef] = Field(
        default_factory=dict,
        description="command 参数名到 typed ValueRef；不得与 args 重名",
    )
    cond_ref: ValueRef | None = None
    cond_cmp: Literal[
        "==", "!=", "exists", "empty", "contains", "not_contains",
        "in", "not_in", ">", ">=", "<", "<=",
    ] = "=="
    cond_value: JsonValue = None
    cond_values: list[JsonValue] = Field(default_factory=list)
    then: list["_StepDraft"] = Field(default_factory=list)
    otherwise: list["_StepDraft"] = Field(default_factory=list)
    items: ValueRef | None = None
    item: str = "item"
    index: str = ""
    body: list["_StepDraft"] = Field(default_factory=list)
    collect: ValueRef | None = None
    into: str = ""
    message: str = ""
    outputs: dict[str, ValueRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_selected_shape(self) -> "_StepDraft":
        if self.op in {"interact", "data"} and not self.goal.strip():
            raise ValueError(f"{self.op} requires goal")
        if self.op == "interact" and not self.success.strip():
            raise ValueError("interact requires success")
        if self.op == "command" and not self.capability:
            raise ValueError("command requires capability")
        if self.op == "if" and self.cond_ref is None:
            raise ValueError("if requires cond_ref")
        if self.op == "foreach":
            if self.items is None:
                raise ValueError("foreach requires items")
            if not self.body:
                raise ValueError("foreach requires a fixed non-empty body")
        return self


class _PlanDraft(BaseModel):
    reasoning: str = Field(
        default="",
        description=(
            "先区分 UI、数据和确定性平台能力，再声明数据依赖与显式 If/ForEach；"
            "不要规划控件、页面路径、SQL、函数或运行时子编排"
        ),
    )
    goal: str = ""
    steps: list[_StepDraft] = Field(default_factory=list)


_StepDraft.model_rebuild()


def _to_stmts(drafts: list[_StepDraft]) -> list[Stmt]:
    statements: list[Stmt] = []
    for draft in drafts:
        if draft.op == "interact":
            statements.append(
                Interact(
                    id=draft.id,
                    bind=draft.bind or None,
                    goal=draft.goal,
                    success=draft.success or draft.goal,
                    inputs=dict(draft.inputs),
                    required_values=dict(draft.required_values),
                    scope=draft.scope,
                    persistence=draft.persistence,
                    returns=dict(draft.returns),
                )
            )
        elif draft.op == "data":
            statements.append(
                Data(
                    id=draft.id,
                    bind=draft.bind or None,
                    goal=draft.goal,
                    inputs=dict(draft.inputs),
                    returns=dict(draft.returns),
                )
            )
        elif draft.op == "command":
            assert draft.capability
            statements.append(
                Command(
                    id=draft.id,
                    bind=draft.bind or None,
                    capability=draft.capability,
                    inputs=dict(draft.inputs),
                    args=dict(draft.args),
                    arg_refs=dict(draft.arg_refs),
                    returns=dict(draft.returns),
                )
            )
        elif draft.op == "if":
            assert draft.cond_ref is not None
            statements.append(
                If(
                    cond=Condition(
                        ref=draft.cond_ref,
                        cmp=draft.cond_cmp,
                        value=draft.cond_value,
                        values=list(draft.cond_values),
                    ),
                    then=_to_stmts(draft.then),
                    otherwise=_to_stmts(draft.otherwise),
                )
            )
        elif draft.op == "foreach":
            assert draft.items is not None
            statements.append(
                ForEach(
                    items=draft.items,
                    item=draft.item or "item",
                    index=draft.index or None,
                    body=_to_stmts(draft.body),
                    collect=draft.collect,
                    into=draft.into or None,
                )
            )
        elif draft.op == "finish":
            statements.append(Finish(message=draft.message, outputs=dict(draft.outputs)))
    return statements


def to_program(draft: _PlanDraft, goal: str = "") -> Program:
    return assign_statement_ids(
        Program(goal=goal or draft.goal, statements=_to_stmts(draft.steps))
    )


__all__ = ["_PlanDraft", "_StepDraft", "_to_stmts", "to_program"]
