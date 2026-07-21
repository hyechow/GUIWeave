"""Compile a user goal into the semantic Program IR."""

from __future__ import annotations

import re
from collections.abc import Callable

from langchain_openai import ChatOpenAI
from pydantic import TypeAdapter
import yaml

from gui_agent.context import ContextBlock
from gui_agent.context.runtime import (
    feedback_block,
    file_reference_block,
    knowledge_block,
    task_goal_block,
)
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.data_types import DataStep, DistinctStep, FieldRef, FilterStep, ProjectStep
from gui_agent.core.llm.messages import assemble_messages
from gui_agent.core.router import IntentResolution
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

from ._decomposer.draft import _PlanDraft, _StepDraft, _to_stmts, to_program
from ._validator.issue import ValidationIssue
from .program import ComputeRef, ForEach, If, Interact, OutputSpec, Program, Stmt, ValueRef
from .validator import validate_program


_SYSTEM = load_prompt_text("task.orchestrator.decomposer")
_REDECOMPOSE_SYSTEM = _SYSTEM + "\n\n" + load_prompt_text("task.orchestrator.redecomposer")
_MAX_REPAIRS = 1
_DATA_STEP_ADAPTER = TypeAdapter(DataStep)
_FIELD_OWNERSHIP_RE = re.compile(
    r"```field_ownership\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE
)


def _active_field_ownership(knowledge: str, goal: str) -> dict | None:
    required = {
        "field", "member_detail_source_field", "member_detail_output_field",
        "owner_identity_source_field", "owner_identity_output_field",
        "owner_identity_transform", "owner_scope", "policy", "output_policy",
    }
    folded_goal = goal.casefold()
    for index, match in enumerate(_FIELD_OWNERSHIP_RE.finditer(knowledge), 1):
        raw = yaml.safe_load(match.group(1))
        if not isinstance(raw, dict) or any(not raw.get(name) for name in required):
            raise ValueError(f"invalid field_ownership contract #{index}")
        if not isinstance(raw["owner_identity_transform"], dict):
            raise ValueError(f"invalid owner_identity_transform in contract #{index}")
        aliases = raw.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        names = [str(raw["field"]), *(str(alias) for alias in aliases)]
        if any(name.casefold() in folded_goal for name in names):
            return raw
    return None


class OrchestratorCompileError(RuntimeError):
    def __init__(self, issues: list[ValidationIssue], program: Program) -> None:
        self.issues = list(issues)
        self.program = program
        super().__init__(
            "orchestrator compile validation failed: "
            + "; ".join(str(issue) for issue in issues[:4])
        )


def _intent_facts(resolution: IntentResolution | None) -> ContextBlock | None:
    if resolution is None or not resolution.entities:
        return None
    lines = []
    for entity in resolution.entities:
        members = list(entity.value_members or [])
        lines.append(
            f"- mention={entity.mention!r}; role={entity.role}; type={entity.type}; "
            f"members={members}; range={(entity.selector or '')!r}; "
            f"search_hint={(entity.search_key or '')!r}"
        )
    return ContextBlock(
        id="runtime.intent_facts",
        budget="required",
        source_type="runtime_state",
        source="intent_resolver",
        ttl="task",
        priority=21,
        content=(
            "## Intent facts\n"
            "这些是检索值、匹配模式与范围的权威合同。需要检索 lookup 实体时，用一个 "
            "lookup macro 引用原始 mention 和语义字段；Compiler 负责完整值精确检索及零结果后的 "
            "search_hint 回退。真实控件和页面路径仍由运行时 Interact 决定。\n"
            + "\n".join(lines)
        ),
    )


def _location_block(site: str, title: str, url: str) -> ContextBlock | None:
    values = [part for part in (site, title, url) if part]
    if not values:
        return None
    return ContextBlock(
        id="runtime.main_location",
        budget="normal",
        source_type="runtime_state",
        source="main_surface",
        ttl="turn",
        priority=30,
        content="## Current main location\n" + " | ".join(values),
    )


def _value_contract_issues(
    program: Program,
    resolution: IntentResolution | None,
) -> list[ValidationIssue]:
    def walk(statements: list[Stmt]):
        for statement in statements:
            yield statement
            if isinstance(statement, If):
                yield from walk(statement.then)
                yield from walk(statement.otherwise)
            elif isinstance(statement, ForEach):
                yield from walk(statement.body)

    allowed_lookups = {
        entity.mention.strip().casefold()
        for entity in (resolution.entities if resolution is not None else [])
        if entity.role == "lookup"
    }
    issues: list[ValidationIssue] = []
    for statement in walk(program.statements):
        if not isinstance(statement, Interact):
            continue
        mention = statement.required_values.get("lookup_entity")
        if isinstance(mention, str) and mention.strip().casefold() not in allowed_lookups:
            issues.append(ValidationIssue(
                "ROUTER_LOOKUP_NOT_DECLARED",
                f"lookup 实体「{mention}」不在 Router lookup facts 中；泛称、输出字段和集合范围"
                "不得包装成实体检索，请删除 lookup macro 并直接编排其真实工作",
                evidence=(mention,),
            ))
    if resolution is None:
        return issues
    payload = program.model_dump_json(exclude={"id"}).casefold()
    for entity in resolution.entities:
        if entity.role in {"target_value", "qualifier_value"}:
            values = list(entity.value_members or []) or [entity.mention]
        elif entity.role == "collection_scope":
            values = [entity.selector or entity.mention]
        else:
            continue
        missing = [value for value in values if str(value).casefold() not in payload]
        if missing:
            issues.append(
                ValidationIssue(
                    "ROUTER_VALUE_NOT_PRESERVED",
                    f"Router 声明的目标值/范围 {missing} 未进入 Program 的语义目标或参数",
                    evidence=tuple(missing),
                )
            )
    return issues


def _draft_data_flow_issues(draft: _PlanDraft) -> list[ValidationIssue]:
    """Keep observation reads separate from deterministic computation."""
    issues: list[ValidationIssue] = []

    def walk(steps: list[_StepDraft]) -> None:
        compute_sources = {
            step.compute_source
            for step in steps
            if step.op == "compute" and step.compute_source
        }
        for previous, current in zip(steps, steps[1:]):
            if previous.op == current.op and current.op in {"data", "compute"}:
                issues.append(ValidationIssue(
                    "DATA_CHAIN_NOT_FUSED",
                    "同一直线 block 中连续数据处理节点之间没有新的 UI、采集或控制流事实；"
                    "请合并为一个 Compute，由它从原始输入完成筛选、分组、聚合、排序、"
                    "排名和最终投影，并只声明最终消费者实际需要的 returns",
                    evidence=(
                        previous.bind or previous.goal,
                        current.bind or current.goal,
                    ),
                ))
        for step in steps:
            if step.op == "data" and step.inputs:
                issues.append(ValidationIssue(
                    "DATA_READ_INPUT_FORBIDDEN",
                    "Data 只能从当前 observation 读取事实或绑定字段，不能消费 typed inputs；"
                    "请把完整确定性逻辑写入一个 Compute 的 compute_steps",
                    evidence=(step.bind or step.goal, *step.inputs),
                ))
            materializes_compute_source = bool(
                step.bind
                and step.bind in compute_sources
                and not step.inputs
                and len(step.returns) == 1
                and next(iter(step.returns.values())).type == "list[record]"
            )
            if (
                step.op == "data"
                and step.coverage in {"complete", "best_effort"}
                and not materializes_compute_source
            ):
                issues.append(ValidationIssue(
                    "DATA_READ_COVERAGE_INVALID",
                    "Data 只能读取 current_view；跨窗口集合必须由 compute(coverage=complete|best_effort) "
                    "声明，Compiler 会生成 Acquire，确定性处理由 Compute 执行",
                    evidence=(step.bind or step.goal, step.coverage),
                ))
            walk(step.then)
            walk(step.otherwise)
            walk(step.body)

    walk(draft.steps)
    return issues


def _apply_ownership_contract(draft: _PlanDraft, contract: dict | None) -> None:
    if not contract:
        return
    if (
        contract["policy"] != "member_then_owner_if_empty"
        or contract["output_policy"] != "distinct_nonempty_values"
    ):
        raise ValueError("unsupported field_ownership policy")
    field = str(contract["field"])
    output = str(contract.get("output_field") or field)
    detail_source = str(contract["member_detail_source_field"])
    detail_output = str(contract["member_detail_output_field"])
    identity_source = str(contract["owner_identity_source_field"])
    identity_output = str(contract["owner_identity_output_field"])
    transform = _DATA_STEP_ADAPTER.validate_python({
        **contract["owner_identity_transform"],
        "field": {"path": [identity_source], "type": "text", "semantic": True},
        "output": identity_output,
    })
    candidates = _StepDraft(
        op="compute",
        bind="__owned_field_candidates",
        goal=f"准备 {field} ownership 候选记录",
        coverage="complete",
        compute_source="records",
        required_fields=[detail_source, identity_source],
        compute_steps=[
            transform,
            ProjectStep(fields={
                detail_output: FieldRef(path=[detail_source], semantic=True),
                identity_output: FieldRef(path=[identity_output]),
            }),
        ],
        compute_outputs={"result": ComputeRef()},
        returns={"result": OutputSpec(
            type="list[record]", fields=[detail_output, identity_output]
        )},
    )
    resolved = _StepDraft.model_construct(
        op="resolve_owned_field",
        bind="__owned_field_values",
        items=ValueRef(var=candidates.bind, path=["result"]),
        member_detail_field=detail_output,
        field=field,
        owner_identity_field=identity_output,
        owner_scope=str(contract["owner_scope"]),
    )
    result = _StepDraft(
        op="compute",
        bind="__owned_field_result",
        goal=f"返回非空且不重复的 {field} 值",
        inputs={"records": ValueRef(var=resolved.bind)},
        compute_source="records",
        compute_steps=[
            FilterStep(field=FieldRef(path=[field]), cmp="exists"),
            ProjectStep(fields={output: FieldRef(path=[field])}),
            DistinctStep(fields=[FieldRef(path=[output])]),
        ],
        compute_outputs={"result": ComputeRef()},
        returns={"result": OutputSpec(type="list[record]", fields=[output])},
    )
    scope = []
    for step in draft.steps:
        if step.op not in {"interact", "lookup"}:
            break
        scope.append(step)
    draft.steps = [
        *scope,
        candidates,
        resolved,
        result,
        _StepDraft(
            op="finish",
            outputs={"result": ValueRef(var=result.bind, path=["result"])},
        ),
    ]


def _compile(
    *,
    system_prompt: str,
    goal: str,
    context_blocks: list[ContextBlock | None],
    context_reports: list[dict] | None,
    resolution: IntentResolution | None,
    label: str,
    ownership_contract: dict | None = None,
    initial_scope: dict[str, dict[str, OutputSpec]] | None = None,
    attempt_observer: Callable[[int, list[ValidationIssue]], None] | None = None,
) -> Program:
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    from llm.provider_config import dashscope_extra_body

    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        extra_body=dashscope_extra_body(cfg.model),
    )
    issues: list[ValidationIssue] = []
    previous = ""
    program = Program(goal=goal)
    for attempt in range(_MAX_REPAIRS + 1):
        messages = assemble_messages(
            system_prompt,
            None,
            human_blocks=[*context_blocks, feedback_block(issues, previous_output=previous)],
            image_resize="none",
            label=label,
            context_reports=context_reports,
            decision_text="",
        )
        draft = invoke_structured(
            llm,
            messages,
            _PlanDraft,
            trace_sink=context_reports,
            trace_label=label,
        )
        previous = draft.model_dump_json(exclude_defaults=True, exclude_none=True)
        _apply_ownership_contract(draft, ownership_contract)
        program = to_program(
            draft,
            goal,
            resolution=resolution,
            initial_collection_binds=frozenset(
                name
                for name, outputs in (initial_scope or {}).items()
                if any(spec.type == "list[record]" for spec in outputs.values())
            ),
        )
        issues = [
            *_draft_data_flow_issues(draft),
            *validate_program(program, initial_scope=initial_scope),
            *_value_contract_issues(program, resolution),
        ]
        if attempt_observer:
            attempt_observer(attempt, list(issues))
        if not issues:
            return program
        if attempt < _MAX_REPAIRS:
            print(f"  [Orchestrator] 语义 Program 有 {len(issues)} 项结构问题，修复一次...")
            for issue in issues:
                print(f"  [Orchestrator]   {issue}")
    raise OrchestratorCompileError(issues, program)


def decompose(
    goal: str,
    *,
    knowledge: str = "",
    file_section: str = "",
    system_prompt: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
    context_reports: list[dict] | None = None,
    corrective_directive: str = "",
    resolution: IntentResolution | None = None,
    attempt_observer: Callable[[int, list[ValidationIssue]], None] | None = None,
    initial_scope: dict[str, dict[str, OutputSpec]] | None = None,
) -> Program:
    ownership_contract = _active_field_ownership(knowledge, goal)
    corrective = (
        ContextBlock(
            id="runtime.corrective_directive",
            budget="required",
            source_type="runtime_state",
            source="program_runtime",
            ttl="turn",
            priority=10,
            content="## Runtime correction\n" + corrective_directive,
        )
        if corrective_directive
        else None
    )
    return _compile(
        system_prompt=system_prompt or _SYSTEM,
        goal=goal,
        context_blocks=[
            task_goal_block(goal),
            _intent_facts(resolution),
            corrective,
            file_reference_block(file_section),
            knowledge_block("app_navigation", knowledge),
            _location_block(current_site, current_title, current_url),
        ],
        context_reports=context_reports,
        resolution=resolution,
        label="orchestrator.decompose",
        ownership_contract=ownership_contract,
        initial_scope=initial_scope,
        attempt_observer=attempt_observer,
    )


def redecompose(
    goal: str,
    *,
    remaining_plan: str = "",
    prior_experience: str = "",
    corrective_directive: str = "",
    available_bindings: dict[str, dict[str, OutputSpec]] | None = None,
    **kwargs,
) -> Program:
    extra = "\n\n".join(
        part
        for part in (
            f"已完成事实：\n{prior_experience}" if prior_experience else "",
            f"剩余语义工作：\n{remaining_plan}" if remaining_plan else "",
            f"运行时纠正：\n{corrective_directive}" if corrective_directive else "",
        )
        if part
    )
    return decompose(
        goal,
        system_prompt=_REDECOMPOSE_SYSTEM,
        corrective_directive=extra,
        initial_scope=available_bindings,
        **kwargs,
    )


__all__ = [
    "OrchestratorCompileError",
    "_PlanDraft",
    "_StepDraft",
    "_to_stmts",
    "decompose",
    "redecompose",
    "to_program",
]
