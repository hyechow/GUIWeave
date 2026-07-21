"""LLM-facing semantic draft and deterministic Program lowering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from ..program import (
    Acquire,
    Command,
    Compute,
    ComputeRef,
    Condition,
    Finish,
    ForEach,
    If,
    Interact,
    ObservationBinding,
    OutputSpec,
    Program,
    Read,
    SourceCheck,
    Stmt,
    ValueRef,
    assign_statement_ids,
)
from gui_agent.core.router import IntentResolution
from gui_agent.core.data_types import BuildRecordStep, ComputeStep, FieldRef


class _StepDraft(BaseModel):
    """Flat structured-output shape; ``op`` selects the relevant fields."""

    op: Literal[
        "interact", "lookup", "read", "compute", "command", "if", "foreach", "finish"
    ] = Field(
        default="interact",
        description=(
            '"interact" | "lookup" | "read" | "compute" | "command" | "if" | '
            '"foreach" | "finish"'
        ),
    )
    bind: str = ""
    goal: str = Field(
        default="",
        description=(
            "interact 的单一 UI 后置条件，或 read 绑定的 observation 事实摘要"
        ),
    )
    required_fields: list[str] = Field(
        default_factory=list,
        description="compute/Acquire 源记录依赖，或 read 要绑定的语义字段",
    )
    reads: dict[str, ObservationBinding] = Field(
        default_factory=dict,
        description="read output -> 当前 observation 中的声明式 fact binding",
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
    observe_fields: list[str] = Field(
        default_factory=list,
        description="interact 只暴露并读取、不得修改的语义字段",
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
    member_detail_field: str = ""
    field: str = ""
    owner_identity_field: str = ""
    owner_match_field: str = ""
    owner_scope: str = ""
    prepare_source: str = Field(
        default="",
        description="data source 字段当前不可读时要达到的线性 UI 后置条件",
    )
    coverage: Literal["current_view", "complete", "best_effort"] | None = Field(
        default=None,
        description=(
            "read 省略时归一为 current_view，且不能使用其他值；"
            "compute 跨窗口集合运算用 complete，"
            "消费已有 typed input 时留空并继承输入合同"
        ),
    )
    persistence: Literal["immediate", "explicit_commit"] = Field(
        default="immediate",
        description="interact: immediate | explicit_commit",
    )
    returns: dict[str, OutputSpec] = Field(
        default_factory=dict,
        description="typed output contract：字段名 → type/required/description",
    )
    compute_source: str = Field(
        default="",
        description="compute: inputs 中作为 record list/table 的确定性变换来源",
    )
    compute_steps: list[ComputeStep] = Field(
        default_factory=list,
        description="compute: 按固定顺序执行的数据内核步骤；字段可标记 semantic=true",
    )
    compute_outputs: dict[str, ComputeRef] = Field(
        default_factory=dict,
        description="compute: output 名到最终 pipeline 结果的路径",
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

    @field_validator("returns", mode="before")
    @classmethod
    def _normalize_single_return(cls, value: Any) -> Any:
        """Accept a common LLM shorthand for one typed output."""
        if isinstance(value, dict) and "type" in value:
            return {"result": value}
        return value

    @field_validator("coverage", mode="before")
    @classmethod
    def _normalize_empty_coverage(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("cond_cmp", mode="before")
    @classmethod
    def _normalize_nonempty_condition(cls, value: Any) -> Any:
        return "exists" if value in {"not_empty", "nonempty", "is_not_empty"} else value

    @model_validator(mode="after")
    def _validate_selected_shape(self) -> "_StepDraft":
        if self.op == "interact" and not (self.goal.strip() or self.success.strip()):
            raise ValueError("interact requires goal or success")
        if self.op == "read" and not self.returns:
            raise ValueError("read requires typed returns")
        if self.op == "read" and not self.inputs and self.coverage is None:
            self.coverage = "current_view"
        if self.op == "compute":
            if not self.compute_steps or not self.compute_outputs or not self.returns:
                raise ValueError("compute requires steps, outputs and typed returns")
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


class _OwnershipScopeStep(BaseModel):
    """LLM-owned collection scope before compiler-owned field resolution."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["interact"] = "interact"
    goal: str
    success: str
    required_values: dict[str, JsonValue] = Field(default_factory=dict)
    scope: str = ""
    persistence: Literal["immediate", "explicit_commit"] = "immediate"


class _OwnershipPlanDraft(BaseModel):
    """Reduced draft used when a field_ownership contract owns data flow."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        default="",
        description=(
            "只说明用户要求的集合范围及其筛选值；字段读取、owner 回退、循环和去重由 Compiler 生成"
        ),
    )
    goal: str = ""
    steps: list[_OwnershipScopeStep] = Field(min_length=1)

    def to_plan(self) -> _PlanDraft:
        return _PlanDraft(
            reasoning=self.reasoning,
            goal=self.goal,
            steps=[_StepDraft.model_validate(step.model_dump()) for step in self.steps],
        )


_StepDraft.model_rebuild()


def _match_count_returns(description: str) -> dict[str, OutputSpec]:
    return {"match_count": OutputSpec(type="number", description=description)}


def _read_summary(draft: _StepDraft) -> str:
    if draft.goal.strip():
        return draft.goal.strip()
    outputs = "、".join(
        spec.description.strip() or name for name, spec in draft.returns.items()
    )
    return f"从当前 observation 直接读取这些 typed outputs：{outputs}"


def _observation_reads(draft: _StepDraft) -> dict[str, ObservationBinding]:
    if draft.reads:
        return dict(draft.reads)
    outputs = list(draft.returns)
    names = (
        list(draft.required_fields)
        if draft.required_fields and len(draft.required_fields) == len(outputs)
        else outputs
    )
    return {
        output: ObservationBinding(
            source=(
                "dataset"
                if draft.returns[output].type == "list[record]"
                else "page"
                if name.casefold() in {"url", "title"}
                else "field"
            ),
            name=("rows" if draft.returns[output].type == "list[record]" else name),
        )
        for output, name in zip(outputs, names, strict=True)
    }


def _compute_required_fields(steps: list[ComputeStep]) -> list[str]:
    """Derive source-field dependencies from compiler-owned semantic references."""

    fields: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            path = value.get("path")
            if (
                value.get("semantic") is True
                and isinstance(path, list)
                and path
                and isinstance(path[0], str)
            ):
                fields.append(path[0])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for step in steps:
        visit(step.model_dump(mode="json"))
    return list(dict.fromkeys(fields))


def _compute_outputs(draft: _StepDraft) -> dict[str, ComputeRef]:
    """Normalize legacy named-result aliases into Compute's one result value."""

    outputs = dict(draft.compute_outputs)
    for name, spec in draft.returns.items():
        ref = outputs.get(name)
        if spec.type == "list[record]" and ref is not None and ref.path:
            outputs[name] = ComputeRef()
    return outputs


@dataclass
class _LoweringContext:
    resolution: IntentResolution | None
    collection_binds: frozenset[str] = frozenset()
    collection_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)
    collection_bindings: dict[str, str] = field(default_factory=dict)
    macro_seq: int = 0

    def next_macro(self, kind: str) -> str:
        self.macro_seq += 1
        return f"__{kind}_{self.macro_seq}"

    def register_collection(
        self,
        bind: str,
        required_fields: list[str],
        bindings: str = "",
    ) -> None:
        self.collection_binds = self.collection_binds | {bind}
        self.collection_fields[bind] = tuple(required_fields)
        if bindings:
            self.collection_bindings[bind] = bindings


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


def _normalize_compute_sources(drafts: list[_StepDraft]) -> None:
    """Fold a redundant complete collection read into its adjacent Compute."""

    for index, (source, consumer) in enumerate(zip(drafts, drafts[1:]), 1):
        collection_outputs = [
            name for name, spec in source.returns.items()
            if spec.type == "list[record]"
        ]
        if not (
            source.op == "read"
            and source.coverage in {"complete", "best_effort"}
            and not source.inputs
            and len(source.returns) == 1
            and len(collection_outputs) == 1
            and consumer.op == "compute"
            and not consumer.inputs
            and consumer.compute_source
            in {"", "records", collection_outputs[0], source.bind}
        ):
            continue
        source.bind = source.bind or f"__compute_source_{index}"
        consumer.compute_source = source.bind
    for step in drafts:
        _normalize_compute_sources(step.then)
        _normalize_compute_sources(step.otherwise)
        _normalize_compute_sources(step.body)


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
    count = Read(
        bind=count_bind,
        reads={
            "match_count": ObservationBinding(source="dataset", name="total_records")
        },
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
        statements.append(Read(
            bind=final_bind,
            reads={
                "match_count": ObservationBinding(source="dataset", name="total_records")
            },
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
    ctx.register_collection(bind, required, final_bind)
    return [
        SourceCheck(
            bind=initial_bind,
            required_fields=required,
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
        SourceCheck(
            bind=final_bind,
            required_fields=required,
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
    _normalize_compute_sources(drafts)
    ctx = _ctx or _LoweringContext(None, _collection_binds(drafts))
    statements: list[Stmt] = []
    declared_outputs = {
        draft.bind: draft.returns
        for draft in drafts
        if draft.bind and draft.returns
    }
    compute_sources = {
        draft.compute_source
        for draft in drafts
        if draft.op == "compute" and draft.compute_source
    }
    for draft in drafts:
        if draft.op == "interact":
            goal = draft.goal.strip() or draft.success.strip()
            statements.append(
                Interact(
                    goal=goal,
                    success=draft.success.strip() or goal,
                    inputs=dict(draft.inputs),
                    required_values=dict(draft.required_values),
                    observe_fields=list(draft.observe_fields),
                    scope=draft.scope,
                    persistence=draft.persistence,
                )
            )
            # The LLM-facing draft may attach a terminal read to an UI step for
            # convenience. Program IR never does: lower it to an adjacent Read
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
                    "op": "read",
                    "goal": (
                        "从上一 UI 后置条件的终态 observation 直接读取："
                        + "、".join(
                            spec.description.strip() or name
                            for name, spec in draft.returns.items()
                        )
                    ),
                    "success": "",
                    "required_values": {},
                    "observe_fields": [],
                    "coverage": coverage,
                })
                statements.extend(_to_stmts([read_draft], _ctx=ctx))
        elif draft.op == "lookup":
            statements.extend(_lookup_stmts(draft, ctx))
        elif draft.op == "resolve_owned_field":
            assert draft.items is not None
            item = draft.item or "item"
            current_bind = ctx.next_macro("current_value")
            owner_bind = ctx.next_macro("owner_value")
            effective_bind = ctx.next_macro("effective_value")
            value = draft.field.strip()
            result_spec = OutputSpec(
                type="record",
                description=f"resolved {value} value",
                fields=[value],
            )
            loop = _StepDraft(
                op="foreach",
                items=draft.items,
                item=item,
                into=draft.bind,
                body=[
                    _StepDraft(
                        op="command",
                        capability="open_url",
                        arg_refs={
                            "url": ValueRef(var=item, path=[draft.member_detail_field.strip()])
                        },
                    ),
                    _StepDraft(
                        op="interact",
                        goal=f"使当前成员详情中的语义字段「{value}」进入可观察视口",
                        success=(
                            f"当前成员详情中的 {value} 已在当前视口中，"
                            "其当前值（包括空值）可直接读取"
                        ),
                        observe_fields=[value],
                    ),
                    _StepDraft(
                        op="read",
                        bind=current_bind,
                        goal=f"从当前成员详情读取 {value}",
                        required_fields=[value],
                        returns={
                            "value": OutputSpec(
                                type="text",
                                required=False,
                                description=f"current member {value}",
                            )
                        },
                    ),
                    _StepDraft(
                        op="if",
                        cond_ref=ValueRef(var=current_bind, path=["value"]),
                        cond_cmp="empty",
                        then=[
                            _StepDraft(
                                op="interact",
                                goal=(
                                    f"使用本次调用 inputs[{draft.owner_match_field!r}] 的精确值定位 "
                                    "owner 记录；owner 范围："
                                    f"{draft.owner_scope.strip()}"
                                ),
                                success=(
                                    f"当前 owner 详情中的 {draft.owner_match_field} 与本次调用输入"
                                    "精确相等，且 owner 范围条件已确认"
                                ),
                                inputs={
                                    draft.owner_match_field: ValueRef(
                                        var=item,
                                        path=[draft.owner_identity_field.strip()],
                                    )
                                },
                            ),
                            _StepDraft(
                                op="interact",
                                goal=f"使 owner 详情中的语义字段「{value}」进入可观察视口",
                                success=(
                                    f"owner 详情中的 {value} 已在当前视口中，"
                                    "其当前值（包括空值）可直接读取"
                                ),
                                observe_fields=[value],
                            ),
                            _StepDraft(
                                op="read",
                                bind=owner_bind,
                                goal=f"从 owner 详情读取 {value}",
                                required_fields=[value],
                                returns={
                                    "value": OutputSpec(
                                        type="text",
                                        required=False,
                                        description=f"owner {value}",
                                    )
                                },
                            ),
                            _StepDraft(
                                op="compute",
                                bind=effective_bind,
                                goal=f"绑定 owner {value} record",
                                inputs={
                                    "value": ValueRef(var=owner_bind, path=["value"])
                                },
                                compute_source="value",
                                compute_steps=[BuildRecordStep(
                                    fields={value: FieldRef(path=[])},
                                )],
                                compute_outputs={"result": ComputeRef(path=[0])},
                                returns={"result": result_spec},
                            ),
                        ],
                        otherwise=[
                            _StepDraft(
                                op="compute",
                                bind=effective_bind,
                                goal=f"绑定当前成员 {value} record",
                                inputs={
                                    "value": ValueRef(var=current_bind, path=["value"])
                                },
                                compute_source="value",
                                compute_steps=[BuildRecordStep(
                                    fields={value: FieldRef(path=[])},
                                )],
                                compute_outputs={"result": ComputeRef(path=[0])},
                                returns={"result": result_spec},
                            )
                        ],
                    ),
                ],
                collect=ValueRef(var=effective_bind, path=["result"]),
            )
            statements.extend(_to_stmts([loop], _ctx=ctx))
        elif draft.op == "read":
            summary = _read_summary(draft)
            materializes_compute_source = bool(
                draft.bind
                and draft.bind in compute_sources
                and draft.coverage in {"complete", "best_effort"}
                and not draft.inputs
                and len(draft.returns) == 1
                and next(iter(draft.returns.values())).type == "list[record]"
            )
            if materializes_compute_source:
                statements.extend(_acquire_stmts(draft, draft.bind, ctx))
                continue
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
                        "goal": f"物化「{summary}」所需的同一业务集合",
                        "returns": {},
                    }),
                    source_bind,
                    ctx,
                ))
                inputs["records"] = ValueRef(var=source_bind, path=["rows"])
            statements.append(
                Read(
                    bind=draft.bind or None,
                    reads=_observation_reads(draft),
                    inputs=inputs,
                    returns=dict(draft.returns),
                )
            )
        elif draft.op == "compute":
            goal = draft.goal.strip() or "计算并产出：" + "、".join(
                spec.description.strip() or name for name, spec in draft.returns.items()
            )
            required_fields = list(dict.fromkeys([
                *draft.required_fields,
                *_compute_required_fields(draft.compute_steps),
            ]))
            inputs = {
                name: _collection_input(ref, ctx)
                for name, ref in draft.inputs.items()
            }
            source = draft.compute_source or "records"
            source_contract = declared_outputs.get(source, {})
            if source not in inputs and source in ctx.collection_fields:
                inputs["records"] = ValueRef(var=source, path=["rows"])
                source = "records"
            elif source not in inputs and len(source_contract) == 1:
                output_name = next(iter(source_contract))
                inputs["records"] = ValueRef(var=source, path=[output_name])
                source = "records"
            has_materialized_input = any(
                ref.var in ctx.collection_binds for ref in inputs.values()
            )
            if draft.coverage in {"complete", "best_effort"} and not has_materialized_input:
                source_bind = ctx.next_macro("collection")
                statements.extend(_acquire_stmts(
                    draft.model_copy(update={
                        "goal": f"物化「{goal}」所需的同一业务集合",
                        "required_fields": required_fields,
                        "returns": {},
                    }),
                    source_bind,
                    ctx,
                ))
                inputs[source] = ValueRef(var=source_bind, path=["rows"])
            if required_fields:
                source_ref = inputs.get(source)
                inspection_bind = (
                    ctx.collection_bindings.get(source_ref.var, "")
                    if source_ref is not None else ""
                )
                if not inspection_bind:
                    inspection_bind = ctx.next_macro("compute_fields")
                    statements.append(SourceCheck(
                        bind=inspection_bind,
                        required_fields=required_fields,
                        inputs=dict(inputs),
                    ))
                inputs["bindings"] = ValueRef(var=inspection_bind, path=["bindings"])
            statements.append(Compute(
                bind=draft.bind or None,
                goal=goal,
                source=source,
                required_fields=required_fields,
                steps=list(draft.compute_steps),
                outputs=_compute_outputs(draft),
                inputs=inputs,
                returns=dict(draft.returns),
            ))
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
