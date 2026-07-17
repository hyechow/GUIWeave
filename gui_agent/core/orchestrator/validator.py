"""Structural validation for the semantic Program IR.

The validator checks only executable shape and typed data flow.  It does not
enforce retrieval recipes, page paths, SQL predicates, controls, or
application-specific business policy.
"""

from __future__ import annotations

from collections.abc import Mapping

from ._validator.issue import ALL_CODES, IssueList, ValidationIssue
from .program import Command, Data, Finish, ForEach, If, Interact, OutputSpec, Program, Stmt, ValueRef


Scope = dict[str, Mapping[str, OutputSpec] | None]


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
        if isinstance(statement, (Interact, Data, Command)):
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
            if statement.returns and not statement.bind:
                issues.add(
                    "RETURNS_WITHOUT_BIND",
                    f"statement「{statement.goal_text}」声明 outputs 但没有 bind",
                    evidence=(statement.id,),
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
                _check_ref(ref, current, issues, site=f"finish.outputs.{name}")
    return current


def validate_program(program: Program, resolution=None) -> IssueList:
    del resolution
    issues = IssueList()
    if not program.statements:
        issues.add("EMPTY_PROGRAM", "Program 不能为空")
        return issues
    _walk(program.statements, {}, issues, set())
    return issues


__all__ = ["ALL_CODES", "IssueList", "ValidationIssue", "validate_program"]
