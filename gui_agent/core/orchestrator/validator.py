"""Structural validation for the semantic Program IR.

The validator checks only executable shape and typed data flow.  It does not
enforce retrieval recipes, page paths, SQL predicates, controls, or
application-specific business policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from ._validator.issue import ALL_CODES, IssueList, ValidationIssue
from .program import (
    Acquire,
    Command,
    Compute,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    Read,
    SourceCheck,
    Stmt,
    ValueRef,
)


Scope = dict[str, Mapping[str, OutputSpec] | None]
_INSPECTION_OUTPUTS = {
    "available": "boolean",
    "bindings": "record",
    "missing_fields": "json",
}


def _executor_nodes(statements: list[Stmt]):
    for statement in statements:
        if isinstance(statement, (Interact, Acquire, Read, SourceCheck, Compute, Command)):
            yield statement
        elif isinstance(statement, If):
            yield from _executor_nodes(statement.then)
            yield from _executor_nodes(statement.otherwise)
        elif isinstance(statement, ForEach):
            yield from _executor_nodes(statement.body)


def _semantic_fields(values: list[str]) -> frozenset[str]:
    return frozenset(value.strip().casefold() for value in values if value.strip())


def _check_acquired_field_flow(program: Program, issues: IssueList) -> None:
    """Check semantic field dependencies without inferring physical UI columns."""
    nodes = list(_executor_nodes(program.statements))
    inspections: dict[str, list[frozenset[str]]] = {}
    for node in nodes:
        if isinstance(node, SourceCheck) and node.bind:
            inspections.setdefault(node.bind, []).append(
                _semantic_fields(node.required_fields)
            )

    acquired: dict[str, list[frozenset[str]]] = {}
    for node in nodes:
        if not isinstance(node, Acquire) or not node.bind or node.source_check is None:
            continue
        checked = inspections.get(node.source_check.var, [])
        if not checked:
            issues.add(
                "ACQUIRE_SOURCE_CHECK_INVALID",
                f"Acquire「{node.goal}」的 source_check 不是 SourceCheck 结果",
                evidence=(node.id, node.source_check.var),
            )
            continue
        acquired.setdefault(node.bind, []).extend(checked)

    for node in nodes:
        if not isinstance(node, Compute):
            continue
        sources = sorted({ref.var for ref in node.inputs.values()} & set(acquired))
        if not sources:
            continue
        required = _semantic_fields(node.required_fields)
        if not required:
            issues.add(
                "COMPUTE_REQUIRED_FIELDS_REQUIRED",
                f"{node.op}「{node.goal}」消费 Acquire 集合 {sources}，必须用 required_fields "
                "声明分组、筛选、排序和最终输出所需的语义源字段",
                evidence=(node.id, *sources),
            )
            continue
        for source in sources:
            for checked in acquired[source]:
                missing = sorted(required - checked)
                if missing:
                    issues.add(
                        "COMPUTE_FIELDS_NOT_ACQUIRED",
                        f"{node.op}「{node.goal}」需要语义字段 {missing}，但 Acquire 集合「{source}」"
                        "引用的 SourceCheck 没有覆盖它们",
                        evidence=(node.id, source, *missing),
                    )


def _check_ref(ref: ValueRef, scope: Scope, issues: IssueList, *, site: str) -> None:
    if ref.var not in scope:
        issues.add(
            "REF_NOT_IN_SCOPE",
            f"{site} 引用了尚未定义的变量「{ref.var}」",
            evidence=(site, ref.var),
        )
        return
    declared = scope[ref.var]
    if declared is not None and ref.path and isinstance(ref.path[0], str):
        field = ref.path[0]
        if field not in declared:
            issues.add(
                "REF_FIELD_NOT_DECLARED",
                f"{site} 引用了「{ref.var}.{field}」，但该 statement 未声明此 output",
                evidence=(site, ref.var, field),
            )


def _walk(
    statements: list[Stmt],
    scope: Scope,
    issues: IssueList,
    ids: set[str],
) -> Scope:
    current = dict(scope)
    for statement in statements:
        if isinstance(statement, (Interact, Acquire, Read, SourceCheck, Compute, Command)):
            if not statement.id:
                issues.add("EMPTY_STATEMENT_GOAL", "executor statement 缺少稳定 id")
            elif statement.id in ids:
                issues.add(
                    "DUPLICATE_STATEMENT_ID",
                    f"statement id「{statement.id}」重复",
                    evidence=(statement.id,),
                )
            else:
                ids.add(statement.id)
            if not statement.goal_text.strip():
                issues.add(
                    "EMPTY_STATEMENT_GOAL",
                    f"{statement.op} statement 缺少语义 goal",
                    evidence=(statement.id,),
                )
            if isinstance(statement, Interact) and not statement.success.strip():
                issues.add(
                    "INTERACT_MISSING_SUCCESS",
                    f"Interact「{statement.goal}」缺少业务 success 合同",
                    evidence=(statement.id,),
                )
            if isinstance(statement, Acquire):
                collection_outputs = [
                    (name, spec) for name, spec in statement.returns.items()
                    if spec.type == "list[record]"
                ]
                if len(statement.returns) != 1 or len(collection_outputs) != 1:
                    issues.add(
                        "ACQUIRE_OUTPUT_CONTRACT",
                        f"Acquire「{statement.goal}」必须且只能声明一个 list[record] output",
                        evidence=(statement.id,),
                    )
                elif collection_outputs[0][1].coverage not in {"complete", "best_effort"}:
                    issues.add(
                        "ACQUIRE_COVERAGE_REQUIRED",
                        f"Acquire「{statement.goal}」的集合 output 必须声明 "
                        "coverage=complete 或 best_effort",
                        evidence=(statement.id, collection_outputs[0][0]),
                    )
                elif collection_outputs[0][1].fields:
                    issues.add(
                        "ACQUIRE_RAW_FIELDS_FORBIDDEN",
                        f"Acquire「{statement.goal}」搬运 observation 原始记录，output 不得声明 fields；"
                        "所需语义源字段只写在 SourceCheck 或 Compute.required_fields",
                        evidence=(
                            statement.id,
                            collection_outputs[0][0],
                            *collection_outputs[0][1].fields,
                        ),
                    )
                if statement.source_check is None:
                    issues.add(
                        "ACQUIRE_SOURCE_CHECK_REQUIRED",
                        f"Acquire「{statement.goal}」启动前必须引用 SourceCheck.available；"
                        "字段不可用分支应先由 Interact 修正界面、重新 inspect，再进入 Acquire",
                        evidence=(statement.id,),
                    )
                else:
                    _check_ref(
                        statement.source_check,
                        current,
                        issues,
                        site=f"{statement.id}.source_check",
                    )
                    declared = current.get(statement.source_check.var)
                    field = (
                        statement.source_check.path[0]
                        if statement.source_check.path
                        and isinstance(statement.source_check.path[0], str)
                        else None
                    )
                    if (
                        field != "available"
                        or declared is None
                        or {name: spec.type for name, spec in declared.items()}
                        != _INSPECTION_OUTPUTS
                    ):
                        issues.add(
                            "ACQUIRE_SOURCE_CHECK_INVALID",
                            "Acquire.source_check 必须引用 SourceCheck 产出的 boolean 字段",
                            evidence=(statement.id,),
                        )
            if isinstance(statement, SourceCheck):
                if not statement.required_fields:
                    issues.add(
                        "SOURCE_CHECK_FIELDS_REQUIRED",
                        "SourceCheck 必须用 required_fields 声明要检查的语义源字段",
                        evidence=(statement.id,),
                    )
                actual = {name: spec.type for name, spec in statement.returns.items()}
                if actual != _INSPECTION_OUTPUTS:
                    issues.add(
                        "SOURCE_CHECK_OUTPUT_CONTRACT",
                        "SourceCheck 必须声明 available:boolean、bindings:record、"
                        "missing_fields:json 三个 outputs",
                        evidence=(statement.id,),
                    )
            if isinstance(statement, Read):
                if statement.inputs:
                    issues.add(
                        "READ_INPUT_FORBIDDEN",
                        "Read 只能绑定当前 observation，不能消费 typed inputs；"
                        "请由编排器把确定性筛选、分组、聚合、排序和投影写成 Compute",
                        evidence=(statement.id, *statement.inputs),
                    )
                invalid_coverage = [
                    name for name, spec in statement.returns.items()
                    if spec.coverage != "current_view"
                ]
                if invalid_coverage:
                    issues.add(
                        "READ_COVERAGE_INVALID",
                        "Read 只能声明 current_view outputs；"
                        f"{invalid_coverage} 的跨窗口覆盖必须由 Acquire + Compute 表达",
                        evidence=(statement.id, *invalid_coverage),
                    )
                if not statement.returns:
                    issues.add(
                        "READ_OUTPUT_REQUIRED",
                        "Read 必须声明从当前 observation 绑定的 typed returns",
                        evidence=(statement.id,),
                    )
                if set(statement.reads) != set(statement.returns):
                    issues.add(
                        "READ_BINDINGS_INVALID",
                        "Read 必须为每个 typed return 声明且仅声明一个 "
                        "observation binding",
                        evidence=(statement.id, *statement.reads),
                    )
                missing_record_fields = [
                    name for name, spec in statement.returns.items()
                    if spec.type == "list[record]" and not spec.fields
                ]
                if missing_record_fields:
                    issues.add(
                        "READ_RECORD_FIELDS_REQUIRED",
                        f"Read 的 list[record] outputs "
                        f"{missing_record_fields} 必须声明 fields；"
                        "例如 fields=['email']，使运行时能校验逐条记录形状",
                        evidence=(statement.id, *missing_record_fields),
                    )
            if isinstance(statement, Compute):
                if statement.source not in statement.inputs:
                    issues.add(
                        "COMPUTE_SOURCE_INPUT_REQUIRED",
                        f"Compute「{statement.goal}」的 source 必须引用 inputs 中的数据来源",
                        evidence=(statement.id, statement.source),
                    )
                extras = sorted(set(statement.outputs) - set(statement.returns))
                missing = sorted(
                    name for name, spec in statement.returns.items()
                    if spec.required and name not in statement.outputs
                )
                if extras or missing:
                    issues.add(
                        "COMPUTE_OUTPUT_CONTRACT",
                        f"Compute「{statement.goal}」的 outputs 与 returns 不一致："
                        f"extras={extras}, missing={missing}",
                        evidence=(statement.id, *extras, *missing),
                    )
            if statement.returns and not statement.bind:
                issues.add(
                    "RETURNS_WITHOUT_BIND",
                    f"statement「{statement.goal_text}」声明 outputs 但没有 bind",
                    evidence=(statement.id,),
                )
            for name, spec in statement.returns.items():
                if spec.coverage in {"complete", "best_effort"} and spec.type != "list[record]":
                    issues.add(
                        "COVERAGE_REQUIRES_LIST_RECORD",
                        f"statement「{statement.goal_text}」声明 output「{name}」"
                        f"coverage={spec.coverage}，但 type={spec.type}；"
                        "coverage 仅可用于 type=list[record]",
                        evidence=(statement.id, name),
                    )
            for name, ref in statement.inputs.items():
                _check_ref(ref, current, issues, site=f"{statement.id}.inputs.{name}")
            if isinstance(statement, Command):
                duplicate_args = sorted(set(statement.args) & set(statement.arg_refs))
                if duplicate_args:
                    issues.add(
                        "COMMAND_ARGUMENT_DUPLICATE",
                        f"Command 参数同时声明 literal 和 ref：{duplicate_args}",
                        evidence=(statement.id, *duplicate_args),
                    )
                for name, ref in statement.arg_refs.items():
                    _check_ref(ref, current, issues, site=f"{statement.id}.arg_refs.{name}")
                required = {"open_url": "url", "launch_app": "app"}.get(statement.capability)
                if required and required not in statement.args and required not in statement.arg_refs:
                    issues.add(
                        "COMMAND_MISSING_ARGUMENT",
                        f"Command {statement.capability} 缺少参数「{required}」",
                        evidence=(statement.id, required),
                    )
                if (
                    required
                    and required in statement.args
                    and not isinstance(statement.args[required], str)
                ):
                    issues.add(
                        "COMMAND_ARGUMENT_INVALID",
                        f"Command {statement.capability} 的 literal 参数「{required}」必须是字符串；"
                        "上游引用请写入 arg_refs",
                        evidence=(statement.id, required),
                    )
                unsupported = sorted(set(statement.returns) - {"url", "title"})
                if unsupported:
                    issues.add(
                        "COMMAND_OUTPUT_UNSUPPORTED",
                        f"Command 只能返回 url/title，不能返回 {unsupported}",
                        evidence=(statement.id, *unsupported),
                    )
            if statement.bind:
                if statement.bind in current:
                    issues.add(
                        "BIND_REDEFINED",
                        f"变量「{statement.bind}」被重复绑定",
                        evidence=(statement.bind,),
                    )
                current[statement.bind] = statement.returns
            continue
        if isinstance(statement, If):
            _check_ref(statement.cond.ref, current, issues, site="if.cond")
            then_scope = _walk(statement.then, current, issues, ids)
            else_scope = _walk(statement.otherwise, current, issues, ids)
            shared = set(then_scope) & set(else_scope)
            current = {
                name: then_scope[name]
                for name in shared
                if then_scope[name] == else_scope[name]
            }
            continue
        if isinstance(statement, ForEach):
            if statement.items.var not in current:
                issues.add(
                    "FOREACH_ITEMS_NOT_IN_SCOPE",
                    f"ForEach items 引用了尚未定义的变量「{statement.items.var}」",
                    evidence=(statement.items.var,),
                )
            else:
                _check_ref(statement.items, current, issues, site="foreach.items")
                declared = current[statement.items.var]
                item_spec = None
                if (
                    declared is not None
                    and statement.items.path
                    and isinstance(statement.items.path[0], str)
                ):
                    item_spec = declared.get(statement.items.path[0])
                if item_spec is None or item_spec.type not in {"list[record]", "json"}:
                    issues.add(
                        "FOREACH_ITEMS_NOT_LIST",
                        "ForEach items 必须引用 type=list[record]（或 json）的具体 output 字段",
                        evidence=(statement.items.var, *statement.items.path),
                    )
            body_scope = dict(current)
            body_scope[statement.item] = None
            if statement.index:
                body_scope[statement.index] = None
            body_scope = _walk(statement.body, body_scope, issues, ids)
            if statement.collect is not None:
                if statement.collect.var not in body_scope:
                    issues.add(
                        "FOREACH_COLLECT_NOT_IN_SCOPE",
                        f"ForEach collect 引用了 body 未定义的变量「{statement.collect.var}」",
                        evidence=(statement.collect.var,),
                    )
                else:
                    _check_ref(statement.collect, body_scope, issues, site="foreach.collect")
            if statement.into:
                if statement.into in current:
                    issues.add(
                        "BIND_REDEFINED",
                        f"变量「{statement.into}」被重复绑定",
                        evidence=(statement.into,),
                    )
                current[statement.into] = None
            continue
        if isinstance(statement, Finish):
            for name, ref in statement.outputs.items():
                site = f"finish.outputs.{name}"
                _check_ref(ref, current, issues, site=site)
    return current


def validate_program(
    program: Program,
    resolution=None,
    *,
    initial_scope: Scope | None = None,
) -> IssueList:
    del resolution
    issues = IssueList()
    if not program.statements:
        issues.add("EMPTY_PROGRAM", "Program 不能为空")
        return issues
    _walk(program.statements, dict(initial_scope or {}), issues, set())
    _check_acquired_field_flow(program, issues)
    return issues


__all__ = ["ALL_CODES", "IssueList", "ValidationIssue", "validate_program"]
