"""LLM-facing semantic draft and deterministic Program lowering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, model_validator

from ..program import (
    Acquire,
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
from gui_agent.core.router import IntentResolution


class _StepDraft(BaseModel):
    """Flat structured-output shape; ``op`` selects the relevant fields."""

    op: Literal[
        "interact", "lookup", "data", "command", "if", "foreach", "finish"
    ] = Field(
        default="interact",
        description=(
            '"interact" | "lookup" | "data" | "command" | '
            '"if" | "foreach" | "finish"'
        ),
    )
    bind: str = ""
    goal: str = Field(
        default="",
        description=(
            "interact/data 的单一语义后置条件；不写页面路径、控件、SQL、表达式或候选分支"
        ),
    )
    required_fields: list[str] = Field(
        default_factory=list,
        description="data 运行前原始记录必须已携带的语义字段",
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
    lookup_entity: str = Field(
        default="",
        description="lookup: Intent facts 中要检索的原始实体 mention",
    )
    lookup_field: str = Field(
        default="",
        description="lookup: 承载实体的语义字段，不是真实列名或控件",
    )
    prepare_source: str = Field(
        default="",
        description="data source 字段当前不可读时要达到的线性 UI 后置条件",
    )
    coverage: Literal["current_view", "complete", "best_effort"] = Field(
        default="current_view",
        description="data source coverage: current_view | complete | best_effort",
    )
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
        if self.op == "interact" and not (self.goal.strip() or self.success.strip()):
            raise ValueError("interact requires goal or success")
        if self.op == "data" and not self.goal.strip() and not self.returns:
            raise ValueError("data requires goal or typed returns")
        if self.op == "lookup" and (
            not self.lookup_entity.strip() or not self.lookup_field.strip()
        ):
            raise ValueError("lookup requires lookup_entity and lookup_field")
        if self.op == "lookup" and (not self.then or not self.otherwise):
            raise ValueError("lookup requires found and not-found branches")
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
            "先区分 UI、采集、数据和确定性平台能力，再声明数据依赖与显式 If/ForEach；"
            "不要规划控件、页面路径、SQL、函数或运行时子编排"
        ),
    )
    goal: str = ""
    steps: list[_StepDraft] = Field(default_factory=list)


_StepDraft.model_rebuild()


def _inspection_returns() -> dict[str, OutputSpec]:
    return {
        "available": OutputSpec(
            type="boolean",
            description="whether every required semantic source field is readable",
        ),
        "bindings": OutputSpec(
            type="record",
            description="semantic source field to runtime field binding",
        ),
        "missing_fields": OutputSpec(
            type="json",
            description="required semantic source fields that are not readable",
        ),
    }


def _match_count_returns(description: str) -> dict[str, OutputSpec]:
    return {"match_count": OutputSpec(type="number", description=description)}


def _data_goal(draft: _StepDraft) -> str:
    if draft.goal.strip():
        return draft.goal.strip()
    outputs = "、".join(
        spec.description.strip() or name for name, spec in draft.returns.items()
    )
    return f"根据声明的数据依赖派生这些 typed outputs：{outputs}"


@dataclass
class _LoweringContext:
    resolution: IntentResolution | None
    collection_binds: frozenset[str] = frozenset()
    collection_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    macro_seq: int = 0

    def next_macro(self, kind: str) -> str:
        self.macro_seq += 1
        return f"__{kind}_{self.macro_seq}"

    def register_collection(self, bind: str, required_fields: list[str]) -> None:
        self.collection_binds = self.collection_binds | {bind}
        self.collection_fields[bind] = tuple(required_fields)


def _collection_input(ref: ValueRef, ctx: _LoweringContext) -> ValueRef:
    if ref.var in ctx.collection_fields and not ref.path:
        return ref.model_copy(update={"path": ["rows"]})
    return ref


def _infer_missing_binds(drafts: list[_StepDraft]) -> None:
    """Name anonymous typed producers from their downstream references."""
    steps: list[_StepDraft] = []

    def visit(items: list[_StepDraft]) -> None:
        for item in items:
            steps.append(item)
            visit(item.then)
            visit(item.otherwise)
            visit(item.body)

    visit(drafts)
    defined = {step.bind for step in steps if step.bind}
    defined.update(step.into for step in steps if step.into)
    defined.update(step.item for step in steps if step.op == "foreach")
    defined.update(step.index for step in steps if step.index)
    references = [
        ref
        for step in steps
        for ref in (
            *step.inputs.values(),
            *step.arg_refs.values(),
            *step.outputs.values(),
            step.cond_ref,
            step.items,
            step.collect,
        )
        if ref is not None and ref.var and ref.var not in defined
    ]
    unresolved = {ref.var for ref in references}
    anonymous = [step for step in steps if step.returns and not step.bind]
    for index, step in enumerate(anonymous, 1):
        fields = set(step.returns)
        candidates = {
            ref.var
            for ref in references
            if ref.var in unresolved
            and (not ref.path or not isinstance(ref.path[0], str) or ref.path[0] in fields)
        }
        if len(candidates) == 1:
            step.bind = candidates.pop()
            unresolved.discard(step.bind)
        elif len(anonymous) == 1 and len(unresolved) == 1:
            step.bind = unresolved.pop()
        else:
            step.bind = f"__value_{index}"


def _collection_binds(drafts: list[_StepDraft]) -> frozenset[str]:
    binds: set[str] = set()
    for draft in drafts:
        if draft.bind and any(spec.type == "list[record]" for spec in draft.returns.values()):
            binds.add(draft.bind)
        binds.update(_collection_binds(draft.then))
        binds.update(_collection_binds(draft.otherwise))
        binds.update(_collection_binds(draft.body))
    return frozenset(binds)


def _lookup_values(
    draft: _StepDraft,
    resolution: IntentResolution | None,
) -> tuple[str, str]:
    """Return authoritative exact and optional fallback values from Router facts."""
    mention = draft.lookup_entity.strip()
    if resolution is None:
        return mention, ""
    for entity in resolution.entities:
        if entity.role != "lookup" or entity.mention.strip().casefold() != mention.casefold():
            continue
        fallback = entity.search_key.strip() if entity.match_mode == "approximate" else ""
        if fallback.casefold() == entity.mention.strip().casefold():
            fallback = ""
        return entity.mention.strip(), fallback
    return mention, ""


def _lookup_stmts(draft: _StepDraft, ctx: _LoweringContext) -> list[Stmt]:
    exact, fallback = _lookup_values(draft, ctx.resolution)
    goal = draft.goal.strip() or (
        f"在语义字段「{draft.lookup_field}」中检索实体「{exact}」，使匹配结果成为当前业务范围"
    )
    values = {
        "lookup_entity": exact,
        "lookup_field": draft.lookup_field,
        "query": exact,
        "match_mode": "exact",
    }
    exact_step = Interact(
        goal=goal,
        success=(
            f"已在语义字段「{draft.lookup_field}」应用完整值「{exact}」并刷新结果；"
            "零条结果也是有效检索结果"
        ),
        inputs=dict(draft.inputs),
        required_values={**dict(draft.required_values), **values},
        scope=draft.scope,
    )
    prefix = ctx.next_macro("lookup")
    count_bind = f"{prefix}_exact"
    count = Data(
        bind=count_bind,
        goal=f"读取使用完整值「{exact}」后的匹配记录数量",
        returns=_match_count_returns("当前精确检索结果中的匹配记录数量"),
    )
    statements: list[Stmt] = [exact_step, count]
    final_bind = count_bind
    if fallback:
        statements.append(If(
            cond=Condition(
                ref=ValueRef(var=count_bind, path=["match_count"]),
                cmp="==",
                value=0,
            ),
            then=[Interact(
                goal=(
                    f"仅当完整值「{exact}」没有结果时，在同一语义字段「{draft.lookup_field}」"
                    f"改用检索提示「{fallback}」并刷新结果"
                ),
                success="模糊回退检索已应用且结果已刷新；零条结果仍是有效检索结果",
                required_values={
                    **dict(draft.required_values),
                    "lookup_entity": exact,
                    "lookup_field": draft.lookup_field,
                    "query": fallback,
                    "match_mode": "approximate",
                },
                scope=draft.scope,
            )],
        ))
        final_bind = f"{prefix}_final"
        statements.append(Data(
            bind=final_bind,
            goal=f"读取实体「{exact}」最终检索结果的匹配记录数量",
            returns=_match_count_returns("精确检索及可选回退后的最终匹配记录数量"),
        ))
    statements.append(If(
            cond=Condition(
                ref=ValueRef(var=final_bind, path=["match_count"]),
                cmp=">",
                value=0,
            ),
            then=_to_stmts(draft.then, _ctx=ctx),
            otherwise=_to_stmts(draft.otherwise, _ctx=ctx),
        ))
    return statements


def _acquire_stmts(
    draft: _StepDraft,
    bind: str,
    ctx: _LoweringContext,
) -> list[Stmt]:
    prefix = ctx.next_macro("source")
    initial_bind = f"{prefix}_initial"
    final_bind = f"{prefix}_final"
    required = list(draft.required_fields)
    goal = draft.goal.strip() or next(
        (
            spec.description.strip()
            for spec in draft.returns.values()
            if spec.type == "list[record]" and spec.description.strip()
        ),
        f"物化当前已圈定的业务集合「{bind}」",
    )
    field_text = "、".join(required) or "下游计算所需语义字段"
    inspect_goal = f"检查当前「{goal}」数据源能否读取：{field_text}"
    prepare_goal = draft.prepare_source.strip() or (
        f"保持当前业务集合范围不变，使其数据源可读取这些语义字段：{field_text}"
    )
    description = next(
        (
            spec.description
            for spec in draft.returns.values()
            if spec.type == "list[record]" and spec.description.strip()
        ),
        goal,
    )
    return [
        Data(
            bind=initial_bind,
            goal=inspect_goal,
            mode="inspect",
            required_fields=required,
            returns=_inspection_returns(),
        ),
        If(
            cond=Condition(
                ref=ValueRef(var=initial_bind, path=["available"]),
                cmp="==",
                value=False,
            ),
            then=[
                Interact(
                    goal=prepare_goal,
                    success=(
                        "当前业务集合范围保持不变，所需语义字段已可读取；"
                        "若当前数据源无法提供则明确报告不可行"
                    ),
                    required_values={"required_fields": required},
                    scope=draft.scope,
                )
            ],
        ),
        Data(
            bind=final_bind,
            goal=inspect_goal,
            mode="inspect",
            required_fields=required,
            returns=_inspection_returns(),
        ),
        Acquire(
            bind=bind,
            goal=goal,
            source_check=ValueRef(var=final_bind, path=["available"]),
            required_fields=required,
            returns={
                "rows": OutputSpec(
                    type="list[record]",
                    coverage=(
                        "complete" if draft.coverage == "current_view" else draft.coverage
                    ),
                    description=description,
                )
            },
        ),
    ]


def _to_stmts(
    drafts: list[_StepDraft],
    *,
    _ctx: _LoweringContext | None = None,
) -> list[Stmt]:
    ctx = _ctx or _LoweringContext(None, _collection_binds(drafts))
    statements: list[Stmt] = []
    for draft in drafts:
        if draft.op == "interact":
            goal = draft.goal.strip() or draft.success.strip()
            statements.append(
                Interact(
                    goal=goal,
                    success=draft.success.strip() or goal,
                    inputs=dict(draft.inputs),
                    required_values=dict(draft.required_values),
                    scope=draft.scope,
                    persistence=draft.persistence,
                )
            )
            # The LLM-facing draft may attach a terminal read to an UI step for
            # convenience.  Program IR never does: lower it to an adjacent Data
            # statement so UI completion and data acceptance remain independent.
            if draft.returns:
                coverage = (
                    "complete"
                    if any(spec.coverage == "complete" for spec in draft.returns.values())
                    else "best_effort"
                    if any(spec.coverage == "best_effort" for spec in draft.returns.values())
                    else "current_view"
                )
                read_draft = draft.model_copy(update={
                    "op": "data",
                    "goal": (
                        "从上一 UI 后置条件的终态观察读取并派生："
                        + "、".join(
                            spec.description.strip() or name
                            for name, spec in draft.returns.items()
                        )
                    ),
                    "success": "",
                    "required_values": {},
                    "coverage": coverage,
                })
                statements.extend(_to_stmts([read_draft], _ctx=ctx))
        elif draft.op == "lookup":
            statements.extend(_lookup_stmts(draft, ctx))
        elif draft.op == "data":
            goal = _data_goal(draft)
            inputs = {
                name: _collection_input(ref, ctx)
                for name, ref in draft.inputs.items()
            }
            has_materialized_input = any(
                ref.var in ctx.collection_binds for ref in inputs.values()
            )
            if draft.coverage in {"complete", "best_effort"} and not has_materialized_input:
                source_bind = ctx.next_macro("collection")
                statements.extend(_acquire_stmts(
                    draft.model_copy(update={
                        "goal": f"物化「{goal}」所需的同一业务集合",
                        "returns": {},
                    }),
                    source_bind,
                    ctx,
                ))
                ctx.register_collection(source_bind, draft.required_fields)
                inputs["records"] = ValueRef(var=source_bind, path=["rows"])
            inherited_fields = list(dict.fromkeys(
                field
                for ref in inputs.values()
                for field in ctx.collection_fields.get(ref.var, ())
            ))
            statements.append(
                Data(
                    bind=draft.bind or None,
                    goal=goal,
                    required_fields=list(draft.required_fields) or inherited_fields,
                    inputs=inputs,
                    returns=dict(draft.returns),
                )
            )
        elif draft.op == "command":
            assert draft.capability
            statements.append(
                Command(
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
                    then=_to_stmts(draft.then, _ctx=ctx),
                    otherwise=_to_stmts(draft.otherwise, _ctx=ctx),
                )
            )
        elif draft.op == "foreach":
            assert draft.items is not None
            statements.append(
                ForEach(
                    items=draft.items,
                    item=draft.item or "item",
                    index=draft.index or None,
                    body=_to_stmts(draft.body, _ctx=ctx),
                    collect=draft.collect,
                    into=draft.into or None,
                )
            )
        elif draft.op == "finish":
            statements.append(Finish(message=draft.message, outputs=dict(draft.outputs)))
    return statements


def to_program(
    draft: _PlanDraft,
    goal: str = "",
    *,
    resolution: IntentResolution | None = None,
    initial_collection_binds: frozenset[str] = frozenset(),
) -> Program:
    _infer_missing_binds(draft.steps)
    ctx = _LoweringContext(
        resolution,
        _collection_binds(draft.steps) | initial_collection_binds,
    )
    program = Program(goal=goal or draft.goal, statements=_to_stmts(draft.steps, _ctx=ctx))
    return assign_statement_ids(program)


__all__ = ["_PlanDraft", "_StepDraft", "_to_stmts", "to_program"]
