"""Reviewed-Python Master for orchestrating autonomous tool-agent Workers.

The program produced here describes task-level control and data flow.  It is
deliberately unable to issue GUI actions: those remain inside ``gui_worker`` and
its screenshot-driven loop.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from jsonschema import Draft202012Validator
from langchain_core.messages import HumanMessage

from gui_agent.core.tool_agent.contracts import (
    DataRequirement,
    ResultRef,
    WorkerOutcome,
    WorkerSpec,
    approach_is_procedural,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import (
    cacheable_system_message,
    diagnostic_prompt_reports,
    input_binding_action,
    message_text,
    response_usage,
)
from gui_agent.core.tool_agent.sandbox import (
    execute_transform,
    validate_transform_row_fields,
    validate_transform_source,
)
from llm.provider_config import chat_request_kwargs


_MASTER_FILENAME = "<tool-agent-master>"
_WORKER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DESTINATION_ONLY_GOAL = re.compile(
    r"^(?!.*(?:多少|数量|总数|统计|汇总|返回|列出|提取|how\s+many|count|total|"
    r"summari[sz]e|extract|list\s+all))"
    r"(?:(?:请|请帮我|帮我)?(?:看|查看|打开|进入|前往|转到|导航到|显示)(?:一下)?"
    r"[^，,；;。！？?!]{0,120}(?:页面|界面|列表|详情|设置|面板|模块|区域|栏目)"
    r"|(?:please\s+)?(?:view|open|go\s+to|navigate\s+to|show)\s+.{0,120}"
    r"(?:page|screen|list|details|settings|dashboard|section|panel))$",
    re.IGNORECASE,
)
_METHOD_BOUND_GOAL = re.compile(
    r"\b(?:using|from|through|via)\s+(?:the\s+)?current\s+.{0,80}"
    r"(?:page|site|app|application|search|screen|interface)\b"
    r"|(?:使用|来自|通过)当前.{0,40}(?:页面|站点|网站|应用|搜索|界面)",
    re.IGNORECASE,
)
_ACTION_SHAPED_SUCCESS = re.compile(
    r"\b(?:button|control|link|field|input|query|search)\b.{0,60}"
    r"\b(?:clicked|tapped|pressed|typed|entered|executed|submitted)\b"
    r"|\b(?:clicked|tapped|pressed|typed|entered|executed|submitted)\b.{0,60}"
    r"\b(?:button|control|link|field|input|query|search)\b"
    r"|(?:按钮|控件|链接|字段|输入框|查询|搜索).{0,30}"
    r"(?:已点击|已按下|已输入|已执行|已提交)",
    re.IGNORECASE,
)
_UI_SURFACE_SUCCESS = re.compile(
    r"\b(?:page|screen|interface|search results?|widget|card|table|dialog)\b.{0,60}"
    r"\b(?:visible|shown|open|opened|loaded|displayed)\b"
    r"|(?:页面|界面|搜索结果|组件|卡片|表格|弹窗).{0,30}(?:可见|显示|打开|已加载)",
    re.IGNORECASE,
)
_EXACT_SCOPE_LITERAL = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|(?P<quote>['\"])(?P<quoted>[^'\"\n]{1,80})(?P=quote)"
)
# English month names; a task that states one requires the datetime filter field
# to carry the month scope so the collector can bound acquisition.
_MONTH_NAMES = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
_CTX_METHODS = {
    "gui_worker",
    "transform",
    "worker_result",
    "finish",
    "fail",
}
_SAFE_METHODS = {
    "append",
    "copy",
    "count",
    "endswith",
    "extend",
    "get",
    "index",
    "items",
    "join",
    "keys",
    "lower",
    "pop",
    "setdefault",
    "sort",
    "split",
    "startswith",
    "strip",
    "upper",
    "values",
}
_SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}
_BANNED_NAMES = {
    "__builtins__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "open",
    "setattr",
    "vars",
}
_BANNED_NODES = (
    ast.AsyncFunctionDef,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Lambda,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.While,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


@dataclass(frozen=True)
class MasterDiagnostic:
    code: str
    message: str
    line: int = 0

    def render(self) -> str:
        location = f"line {self.line}" if self.line else "source"
        return f"[{self.code}] {location}: {self.message}"


@dataclass(frozen=True)
class MasterProgram:
    source: str
    attempts: int


@dataclass(frozen=True)
class MasterTerminal:
    phase: Literal["completed", "failed"]
    summary: str
    result_ref: str = ""
    effect: Literal["mutation", "data", "ui_state", "none"] = "none"


@dataclass(frozen=True)
class MasterExecution:
    terminal: MasterTerminal | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.terminal is not None and not self.error


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    signature: str
    outcome: WorkerOutcome


class MasterCompileError(ValueError):
    pass


class _ProgramHalt(BaseException):
    pass


def _diagnostic(code: str, message: str, node: ast.AST | None = None) -> MasterDiagnostic:
    return MasterDiagnostic(code=code, message=message, line=getattr(node, "lineno", 0))


class _StaticScalarFolder(ast.NodeTransformer):
    """Resolve earlier top-level scalar aliases without executing Master code."""

    def __init__(self, values: dict[str, str | int | float | bool | None]) -> None:
        self.values = values

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if isinstance(node.ctx, ast.Load) and node.id in self.values:
            return ast.copy_location(ast.Constant(self.values[node.id]), node)
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        node = self.generic_visit(node)
        pieces: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                pieces.append(item.value)
                continue
            if not isinstance(item, ast.FormattedValue) or not isinstance(
                item.value, ast.Constant
            ):
                return node
            value = item.value.value
            if item.conversion == ord("r"):
                value = repr(value)
            elif item.conversion == ord("a"):
                value = ascii(value)
            elif item.conversion == ord("s"):
                value = str(value)
            elif item.conversion != -1:
                return node
            if item.format_spec is not None:
                spec = self.visit(item.format_spec)
                if not isinstance(spec, ast.Constant) or not isinstance(spec.value, str):
                    return node
                try:
                    value = format(value, spec.value)
                except (TypeError, ValueError):
                    return node
            pieces.append(str(value))
        return ast.copy_location(ast.Constant("".join(pieces)), node)


def _fold_static_scalar_aliases(tree: ast.Module) -> None:
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != "run":
        return
    values: dict[str, str | int | float | bool | None] = {}
    folder = _StaticScalarFolder(values)
    for statement in functions[0].body:
        folder.visit(statement)
        targets: list[ast.expr] = []
        value_node: ast.expr | None = None
        if isinstance(statement, ast.Assign):
            targets, value_node = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets, value_node = [statement.target], statement.value
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if len(names) != 1 or value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            values.pop(names[0], None)
        else:
            if value is None or isinstance(value, (str, int, float, bool)):
                values[names[0]] = value
            else:
                values.pop(names[0], None)


def _literal_keyword(call: ast.Call, name: str) -> Any:
    keyword = next((item for item in call.keywords if item.arg == name), None)
    if keyword is None:
        raise ValueError(f"missing required keyword {name!r}")
    try:
        return ast.literal_eval(keyword.value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise ValueError(f"keyword {name!r} must be a literal value") from exc


def _validate_worker_id(call: ast.Call) -> list[MasterDiagnostic]:
    try:
        worker_id = _literal_keyword(call, "worker_id")
    except ValueError as exc:
        return [_diagnostic("WORKER_ID", str(exc), call)]
    if not isinstance(worker_id, str) or _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
        return [_diagnostic("WORKER_ID", "worker_id must be a stable snake_case identifier", call)]
    return []


def _validate_gui_worker_call(
    call: ast.Call,
    *,
    platform_context: dict[str, Any] | None = None,
    user_goal: str = "",
) -> list[MasterDiagnostic]:
    del platform_context
    diagnostics = _validate_worker_id(call)
    required = {
        "goal",
        "success_criteria",
        "approach",
    }
    values: dict[str, Any] = {}
    for name in required:
        try:
            values[name] = _literal_keyword(call, name)
        except ValueError as exc:
            diagnostics.append(_diagnostic("GUI_WORKER_LITERAL", str(exc), call))
    if diagnostics:
        return diagnostics
    goal = str(values["goal"] or "")
    success_criteria = values["success_criteria"]
    approach = str(values["approach"] or "")
    if _METHOD_BOUND_GOAL.search(goal) and goal.casefold() != user_goal.casefold():
        diagnostics.append(_diagnostic(
            "WORKER_GOAL_BOUNDARY",
            "immutable goal contains a current implementation method; keep the semantic "
            "goal provider-neutral and move the initial source or mechanism to approach",
            call,
        ))
    criteria = (
        [success_criteria]
        if isinstance(success_criteria, str)
        else success_criteria
        if isinstance(success_criteria, list)
        else []
    )
    if any(_ACTION_SHAPED_SUCCESS.search(str(item or "")) for item in criteria):
        diagnostics.append(_diagnostic(
            "WORKER_SUCCESS_BOUNDARY",
            "success_criteria contain an action or query execution step; keep only "
            "externally checkable semantic outcomes",
            call,
        ))
    if approach_is_procedural(approach):
        diagnostics.append(_diagnostic(
            "WORKER_APPROACH_BOUNDARY",
            "approach contains an action, action argument, URL, or GUI procedure; "
            "state one source or implementation method and leave actions to Worker",
            call,
        ))
    if any(item.arg == "data_requirements" for item in call.keywords):
        try:
            values["data_requirements"] = _literal_keyword(call, "data_requirements")
        except ValueError as exc:
            diagnostics.append(_diagnostic("GUI_WORKER_LITERAL", str(exc), call))
    else:
        try:
            explicit_profile = _literal_keyword(call, "profile")
        except ValueError:
            explicit_profile = None
        if explicit_profile == "operator":
            values["data_requirements"] = []
        else:
            diagnostics.append(_diagnostic(
                "GUI_WORKER_LITERAL",
                "missing required keyword 'data_requirements' unless profile='operator'",
                call,
            ))
    if values.get("data_requirements") and any(
        _UI_SURFACE_SUCCESS.search(str(item or "")) for item in criteria
    ):
        diagnostics.append(_diagnostic(
            "WORKER_SUCCESS_BOUNDARY",
            "collector success_criteria must state collected semantic evidence, not an "
            "intermediate page, search result, widget, card, table, or dialog state",
            call,
        ))
    if any(item.arg == "input_bindings" for item in call.keywords):
        try:
            values["input_bindings"] = _literal_keyword(call, "input_bindings")
        except ValueError as exc:
            diagnostics.append(_diagnostic("GUI_WORKER_LITERAL", str(exc), call))
            values["input_bindings"] = []
    else:
        values["input_bindings"] = []
    contract_criteria = (
        [str(values["success_criteria"])]
        if isinstance(values["success_criteria"], str)
        else [str(item) for item in values["success_criteria"]]
        if isinstance(values["success_criteria"], list)
        else []
    )
    for requirement in values.get("data_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        contract_text = "\n".join([
            str(values["goal"]),
            *(
                criterion for criterion in contract_criteria
                if not _ACTION_SHAPED_SUCCESS.search(criterion)
            ),
            str(requirement.get("description") or ""),
        ])
        filter_text = json.dumps(
            requirement.get("filters") or {},
            ensure_ascii=False,
        ).casefold()
        exact_literals = {
            (match.group("quoted") or match.group(0)).strip()
            for match in _EXACT_SCOPE_LITERAL.finditer(contract_text)
        }
        unbound_literals = {
            item for item in exact_literals
            if item.casefold() not in filter_text
        }
        if unbound_literals:
            diagnostics.append(_diagnostic(
                "DATA_FILTER_BOUNDARY",
                "exact record-selection values must be frozen in data requirement "
                f"filters, not only prose or a later transform: {sorted(unbound_literals)}",
                call,
            ))
        contract_lower = contract_text.casefold()
        has_date_scope = (
            any(month in contract_lower for month in _MONTH_NAMES)
            or bool(re.search(r"\b20\d{2}\b", contract_text))
        )
        if has_date_scope:
            filter_values = json.dumps(
                requirement.get("filters") or {}, ensure_ascii=False
            )
            filter_lower = filter_values.casefold()
            filter_has_date = bool(
                re.search(
                    r"20\d{2}|\b\d{1,2}[-/]\d{1,2}\b|\b\d{4}-\d{2}\b",
                    filter_values,
                )
                or any(month in filter_lower for month in _MONTH_NAMES)
            )
            if not filter_has_date:
                diagnostics.append(_diagnostic(
                    "DATA_FILTER_DATE_SCOPE",
                    "the task text states a month/date scope but no filter value carries "
                    "a date constraint; freeze the date range in filters (e.g. "
                    "{'Event start time': '2025-10-*'}) so the collector can bound "
                    "acquisition",
                    call,
                ))
    try:
        profile = None
        if any(item.arg == "profile" for item in call.keywords):
            profile = _literal_keyword(call, "profile")
        binding_inputs = {
            str(item.get("input") or "")
            for item in values["input_bindings"]
            if isinstance(item, dict)
        }
        spec = WorkerSpec.model_validate(
            {
                "profile": profile,
                "goal": values["goal"],
                "success_criteria": (
                    [values["success_criteria"]]
                    if isinstance(values["success_criteria"], str)
                    else values["success_criteria"]
                ),
                "input_refs": {
                    name: f"result:static:{name}"
                    for name in binding_inputs
                    if name
                },
                "input_bindings": values["input_bindings"],
                "data_requirements": values.get("data_requirements") or [],
                "strategy": {
                    "approach": values["approach"],
                },
            }
        )
        for requirement in spec.data_requirements:
            Draft202012Validator.check_schema(requirement.row_schema)
        for binding in spec.input_bindings:
            input_binding_action(binding)
    except Exception as exc:  # noqa: BLE001 - surfaced as a compile diagnostic
        diagnostics.append(_diagnostic("GUI_WORKER_SPEC", str(exc), call))
    input_keyword = next((item for item in call.keywords if item.arg == "input_refs"), None)
    input_names: set[str] = set()
    if input_keyword is not None:
        if isinstance(input_keyword.value, ast.Constant) and input_keyword.value.value is None:
            pass
        elif not isinstance(input_keyword.value, ast.Dict):
            diagnostics.append(_diagnostic(
                "GUI_WORKER_INPUT_REFS",
                "input_refs must be an inline dict of name: result_descriptor['ref']",
                input_keyword.value,
            ))
        else:
            for key, value in zip(
                input_keyword.value.keys,
                input_keyword.value.values,
                strict=True,
            ):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    diagnostics.append(_diagnostic(
                        "GUI_WORKER_INPUT_REFS",
                        "input_refs names must be literal strings",
                        key,
                    ))
                    continue
                input_names.add(key.value)
                if _subscript_key(value) != "ref":
                    diagnostics.append(_diagnostic(
                        "REF_VALUE_REQUIRED",
                        "gui_worker input_refs require direct ResultRef descriptor['ref'] "
                        "values; a value used only for visual navigation or a conditional "
                        "GUI branch must stay inside one cohesive operator",
                        value,
                    ))
    binding_input_names = {
        str(binding.get("input") or "")
        for binding in values.get("input_bindings") or []
        if isinstance(binding, dict)
    }
    unknown_inputs = binding_input_names.difference(input_names)
    if unknown_inputs:
        diagnostics.append(_diagnostic(
            "GUI_WORKER_INPUT_BINDING",
            f"input_bindings reference undeclared input_refs: {sorted(unknown_inputs)}",
            call,
        ))
    unused_inputs = input_names.difference(binding_input_names)
    if unused_inputs:
        diagnostics.append(_diagnostic(
            "GUI_WORKER_INPUT_BINDING",
            "input_refs must be consumed by deterministic input_bindings: "
            f"{sorted(unused_inputs)}; merge visual-only or conditional dependencies "
            "into one cohesive operator",
            call,
        ))
    return diagnostics


def _validate_transform_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics: list[MasterDiagnostic] = []
    for name in ("transform_id", "source", "result_schema"):
        try:
            value = _literal_keyword(call, name)
            if name == "transform_id" and (
                not isinstance(value, str) or _WORKER_ID_PATTERN.fullmatch(value) is None
            ):
                raise ValueError("transform_id must be a stable snake_case identifier")
            if name == "source":
                if not isinstance(value, str):
                    raise ValueError("source must be a string")
                validate_transform_source(value)
            if name == "result_schema":
                Draft202012Validator.check_schema(value)
        except Exception as exc:  # noqa: BLE001 - one deterministic diagnostic channel
            diagnostics.append(_diagnostic("TRANSFORM_SPEC", f"{name}: {exc}", call))
    if not any(item.arg == "inputs" for item in call.keywords):
        diagnostics.append(_diagnostic("TRANSFORM_SPEC", "missing required keyword 'inputs'", call))
    else:
        inputs = next(item.value for item in call.keywords if item.arg == "inputs")
        if isinstance(inputs, (ast.List, ast.Tuple)):
            for item in inputs.elts:
                if _subscript_key(item) in {"collection_ref", "result_ref"}:
                    diagnostics.append(_diagnostic(
                        "REF_VALUE_REQUIRED",
                        "ctx.transform inputs require descriptor['ref'], not a descriptor object",
                        item,
                    ))
    return diagnostics


def _subscript_key(node: ast.AST) -> str:
    if not isinstance(node, ast.Subscript):
        return ""
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return ""


def _base_name(node: ast.AST) -> str:
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _subscript_path(node: ast.AST) -> tuple[str, tuple[str, ...]]:
    """Return the root name and literal string-key path for a reviewed expression."""

    keys: list[str] = []
    while isinstance(node, ast.Subscript):
        key = _subscript_key(node)
        if not key:
            return "", ()
        keys.append(key)
        node = node.value
    if not isinstance(node, ast.Name):
        return "", ()
    return node.id, tuple(reversed(keys))


def _ctx_call(node: ast.AST, method: str) -> ast.Call | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if not (
        isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ctx"
        and node.func.attr == method
    ):
        return None
    return node


def _named_assignments(tree: ast.AST) -> list[tuple[str, ast.Assign]]:
    """Return simple-name assignments in source order for static data-flow checks."""

    assignments = [
        (node.targets[0].id, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ]
    return sorted(assignments, key=lambda item: getattr(item[1], "lineno", 0))


def _collector_requirements(call: ast.Call) -> list[dict[str, Any]]:
    """Return literal requirements only when a reviewed call is a collector."""

    try:
        requirements = _literal_keyword(call, "data_requirements")
        profile = (
            _literal_keyword(call, "profile")
            if any(item.arg == "profile" for item in call.keywords)
            else None
        )
    except ValueError:
        return []
    return requirements if profile != "operator" and isinstance(requirements, list) else []


def _static_flow_diagnostics(tree: ast.AST) -> list[MasterDiagnostic]:
    """Build one ordered symbol flow for Worker and transform contracts."""

    # name -> (row schema, unresolved descriptor path, collector)
    rows: dict[str, tuple[dict[str, Any], tuple[str, ...], str]] = {}
    collectors: dict[str, ast.Call] = {}
    operators: dict[str, ast.Call] = {}
    transforms: set[str] = set()
    array_results: set[str] = set()
    pending_results: set[str] = set()
    consumed_collectors: set[str] = set()
    diagnostics: list[MasterDiagnostic] = []

    for name, assignment in _named_assignments(tree):
        worker = _ctx_call(assignment.value, "gui_worker")
        if worker is not None:
            requirements = _collector_requirements(worker)
            if requirements:
                collectors[name] = worker
                try:
                    requirement = DataRequirement.model_validate(requirements[0])
                    rows[name] = (
                        requirement.row_schema,
                        ("collection_ref", "ref"),
                        name,
                    )
                except Exception:
                    pass
            else:
                operators[name] = worker
            keyword = next(
                (item for item in worker.keywords if item.arg == "input_refs"), None,
            )
            routed = {
                _base_name(value) for value in keyword.value.values
            } if keyword is not None and isinstance(keyword.value, ast.Dict) else set()
            missing = pending_results.difference(routed)
            if missing:
                diagnostics.append(_diagnostic(
                    "RESULT_REF_UNROUTED",
                    "ResultRefs computed before a GUI Worker must be routed through "
                    f"that call's input_refs: {sorted(missing)}",
                    worker,
                ))
            routed_arrays = sorted(routed.intersection(array_results))
            try:
                bindings = _literal_keyword(worker, "input_bindings")
            except ValueError:
                bindings = []
            # An array ref routed to a Worker is consumed element-wise by its
            # input_bindings: each binding's `input` names an input_refs key, and
            # its `path` selects one field of the current element. Map binding
            # `input` keys back to the routed ref's base name so a binding that
            # consumes an array ref is recognized.
            ref_by_input_key: dict[str, str] = {}
            input_refs_keyword = next(
                (item for item in worker.keywords if item.arg == "input_refs"),
                None,
            )
            if input_refs_keyword is not None and isinstance(input_refs_keyword.value, ast.Dict):
                ref_dict = input_refs_keyword.value
                for ref_key, ref_value in zip(ref_dict.keys, ref_dict.values):
                    key = getattr(ref_key, "value", ref_key)
                    ref_by_input_key[str(key)] = _base_name(ref_value)
            bound_inputs = {
                ref_by_input_key.get(str(item.get("input") or ""), str(item.get("input") or ""))
                for item in bindings
                if isinstance(item, dict)
            } if isinstance(bindings, list) else set()
            unconsumed_arrays = [
                ref for ref in routed_arrays if ref not in bound_inputs
            ]
            bad_paths = [
                str(item.get("name") or "")
                for item in bindings
                if isinstance(item, dict)
                and isinstance(item.get("path"), str)
            ]
            if unconsumed_arrays:
                diagnostics.append(_diagnostic(
                    "WORKER_ARRAY_INPUT_UNSUPPORTED",
                    "GUI Worker receives array ResultRefs "
                    f"from {unconsumed_arrays}; bind each element's field with an "
                    "input_binding whose `input` points at the array ref and whose "
                    "`path` is a list of keys into one element; the Worker calls "
                    "complete after each element",
                    worker,
                ))
            for name in bad_paths:
                diagnostics.append(_diagnostic(
                    "BINDING_PATH_LIST",
                    f"input binding {name!r} uses a string path; path must be a "
                    "list of keys selecting one field of the current array element",
                    worker,
                ))
            pending_results.difference_update(routed)
            continue

        transform = _ctx_call(assignment.value, "transform")
        if transform is not None:
            transforms.add(name)
            inputs = next(
                (item.value for item in transform.keywords if item.arg == "inputs"), None,
            )
            schemas: list[dict[str, Any]] = []
            if isinstance(inputs, (ast.List, ast.Tuple)):
                pending_results.difference_update(_base_name(item) for item in inputs.elts)
                for item in inputs.elts:
                    base, path = _subscript_path(item)
                    if base in rows and path == rows[base][1]:
                        schema, _, collector = rows[base]
                        schemas.append(schema)
                        if collector:
                            consumed_collectors.add(collector)
            if schemas:
                combined = {
                    "type": "object",
                    "properties": {
                        key: value
                        for schema in schemas
                        for key, value in (schema.get("properties") or {}).items()
                    },
                }
                try:
                    validate_transform_row_fields(
                        _literal_keyword(transform, "source"), combined,
                    )
                except Exception as exc:
                    diagnostics.append(_diagnostic(
                        "TRANSFORM_INPUT_SCHEMA", str(exc), transform,
                    ))
            try:
                schema = _literal_keyword(transform, "result_schema")
            except ValueError:
                schema = None
            if isinstance(schema, dict):
                items = schema.get("items")
                if schema.get("type") == "array" and isinstance(items, dict):
                    rows[name] = (items, ("ref",), "")
                if _schema_contains_array(schema):
                    array_results.add(name)
            pending_results.add(name)
            continue

        base, path = _subscript_path(assignment.value)
        if base in rows and path == rows[base][1]:
            schema, _, collector = rows[base]
            rows[name] = (schema, (), collector)
        elif isinstance(assignment.value, ast.Name):
            source = rows.get(assignment.value.id)
            if source is not None and source[1] == ():
                rows[name] = source

    for node in ast.walk(tree):
        base, path = _subscript_path(node)
        if base in operators and path and path[0] == "collection_ref":
            diagnostics.append(_diagnostic(
                "OPERATOR_COLLECTION_REF",
                f"operator {base!r} cannot produce collection_ref; make the cohesive "
                "Worker a collector with one data requirement when downstream Python "
                "needs observed rows",
                node,
            ))
        finish = _ctx_call(node, "finish")
        if finish is None:
            continue
        value: ast.AST | None = finish.args[0] if finish.args else None
        keyword = next((item for item in finish.keywords if item.arg == "result_ref"), None)
        value = keyword.value if keyword is not None else value
        if value is not None:
            base, path = _subscript_path(value)
            row = rows.get(base)
            collector = (
                row[2]
                if row is not None and path == row[1] and row[2]
                else base
                if base in collectors and path == ("collection_ref", "ref")
                else ""
            )
            if collector:
                effect = next(
                    (
                        item.value.value
                        for item in finish.keywords
                        if item.arg == "effect"
                        and isinstance(item.value, ast.Constant)
                        and isinstance(item.value.value, str)
                    ),
                    None,
                )
                if effect == "data":
                    consumed_collectors.add(collector)
                else:
                    diagnostics.append(_diagnostic(
                        "COLLECTION_FINISH_EFFECT",
                        "a CollectionRef may be finished directly only with effect='data'",
                        finish,
                    ))
        if isinstance(value, ast.Name) and value.id in transforms:
            diagnostics.append(_diagnostic(
                "REF_VALUE_REQUIRED",
                f"ctx.finish requires {value.id}['ref'], not the ResultRef descriptor",
                value,
            ))
    diagnostics.extend(
        _diagnostic(
            "COLLECTOR_RESULT_UNUSED",
            f"collector {name!r} must route its collection_ref into ctx.transform "
            "or finish it directly as data; use one cohesive operator when the Worker "
            "only locates/navigates to a record",
            call,
        )
        for name, call in collectors.items()
        if name not in consumed_collectors
    )
    return diagnostics


def _schema_contains_array(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("type") == "array" or any(
            _schema_contains_array(item) for item in value.values()
        )
    return isinstance(value, list) and any(map(_schema_contains_array, value))


def _validate_finish_call(call: ast.Call) -> list[MasterDiagnostic]:
    diagnostics: list[MasterDiagnostic] = []
    unknown_keywords = {
        item.arg for item in call.keywords if item.arg not in {"result_ref", "effect"}
    }
    if unknown_keywords:
        diagnostics.append(_diagnostic(
            "FINISH_SIGNATURE",
            f"ctx.finish received unknown keyword arguments: {sorted(unknown_keywords)}",
            call,
        ))
    if len(call.args) > 1:
        diagnostics.append(_diagnostic(
            "FINISH_SIGNATURE",
            "ctx.finish accepts one positional result ref; effect must be a keyword",
            call,
        ))
    value: ast.AST | None = call.args[0] if call.args else None
    keyword = next((item for item in call.keywords if item.arg == "result_ref"), None)
    if keyword is not None:
        value = keyword.value
    if value is None:
        diagnostics.append(_diagnostic(
            "FINISH_SIGNATURE", "ctx.finish requires a runtime data ref string", call
        ))
    elif _subscript_key(value) != "ref":
        descriptor = _subscript_key(value) in {"collection_ref", "result_ref"}
        diagnostics.append(_diagnostic(
            "REF_VALUE_REQUIRED",
            "ctx.finish requires descriptor['ref'], not the descriptor itself"
            if descriptor
            else "ctx.finish requires an exact ResultRef or CollectionRef string expression "
            "such as result['ref'] or outcome['collection_ref']['ref']; literals, None, "
            "descriptors, and unproven aliases are invalid",
            value,
        ))
    effect_keyword = next((item for item in call.keywords if item.arg == "effect"), None)
    effect = (
        effect_keyword.value.value
        if effect_keyword is not None
        and isinstance(effect_keyword.value, ast.Constant)
        and isinstance(effect_keyword.value.value, str)
        else None
    )
    if effect not in {"mutation", "data", "ui_state"}:
        diagnostics.append(_diagnostic(
            "FINISH_EFFECT",
            "ctx.finish requires literal effect='mutation', 'data', or 'ui_state'",
            call,
        ))
    return diagnostics


def validate_master_source(
    source: str,
    *,
    platform_context: dict[str, Any] | None = None,
    user_goal: str = "",
) -> list[MasterDiagnostic]:
    """Validate one restricted Worker-orchestration program."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        return [MasterDiagnostic("SYNTAX", str(exc), exc.lineno or 0)]
    _fold_static_scalar_aliases(tree)

    diagnostics: list[MasterDiagnostic] = []
    goal = user_goal.strip().rstrip("。.!！?").strip()
    destination_only = bool(_DESTINATION_ONLY_GOAL.fullmatch(goal))
    functions = [item for item in tree.body if isinstance(item, ast.FunctionDef)]
    if len(tree.body) != 1 or len(functions) != 1 or functions[0].name != "run":
        diagnostics.append(_diagnostic("PROGRAM_SHAPE", "source must contain exactly def run(ctx):"))
    else:
        fn = functions[0]
        if (
            len(fn.args.args) != 1
            or fn.args.args[0].arg != "ctx"
            or fn.args.vararg is not None
            or fn.args.kwarg is not None
            or fn.args.kwonlyargs
            or fn.decorator_list
        ):
            diagnostics.append(_diagnostic("RUN_SIGNATURE", "run must accept exactly one argument named ctx", fn))

    terminal_calls = 0
    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            diagnostics.append(_diagnostic("UNSAFE_SYNTAX", f"{type(node).__name__} is disallowed", node))
        if isinstance(node, ast.Name) and node.id in _BANNED_NAMES:
            diagnostics.append(_diagnostic("UNSAFE_NAME", f"{node.id!r} is disallowed", node))
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                diagnostics.append(_diagnostic("PRIVATE_ATTRIBUTE", "private/dunder access is disallowed", node))
            elif not (
                isinstance(node.value, ast.Name)
                and node.value.id == "ctx"
                or node.attr in _SAFE_METHODS
            ):
                diagnostics.append(_diagnostic(
                    "ATTRIBUTE_ACCESS",
                    "Master values are JSON-like dicts; use ['ref'] rather than object attributes",
                    node,
                ))
        if not isinstance(node, ast.Call):
            continue
        if any(item.arg is None for item in node.keywords):
            diagnostics.append(_diagnostic("KEYWORD_SPLAT", "**kwargs calls are disallowed", node))
        if isinstance(node.func, ast.Name):
            if node.func.id not in _SAFE_BUILTINS:
                diagnostics.append(_diagnostic("UNSAFE_CALL", f"call to {node.func.id!r} is disallowed", node))
            continue
        if not isinstance(node.func, ast.Attribute):
            diagnostics.append(_diagnostic("UNSAFE_CALL", "indirect calls are disallowed", node))
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id == "ctx":
            method = node.func.attr
            if method not in _CTX_METHODS:
                diagnostics.append(_diagnostic("UNKNOWN_CTX_API", f"ctx.{method} is not available", node))
                continue
            if method in {"gui_worker", "transform"} and node.args:
                diagnostics.append(_diagnostic("CALL_SIGNATURE", f"ctx.{method} accepts keyword arguments only", node))
            if method == "gui_worker":
                diagnostics.extend(_validate_gui_worker_call(
                    node,
                    platform_context=platform_context,
                    user_goal=user_goal,
                ))
                if destination_only:
                    try:
                        requirements = _literal_keyword(node, "data_requirements")
                        profile = (
                            _literal_keyword(node, "profile")
                            if any(item.arg == "profile" for item in node.keywords)
                            else None
                        )
                    except ValueError:
                        pass
                    else:
                        if profile == "collector" or requirements:
                            diagnostics.append(_diagnostic(
                                "TASK_INTENT",
                                "destination-only requests require an operator with no data",
                                node,
                            ))
            elif method == "transform":
                diagnostics.extend(_validate_transform_call(node))
            elif method in {"finish", "fail"}:
                terminal_calls += 1
                if method == "finish":
                    diagnostics.extend(_validate_finish_call(node))
                    if destination_only:
                        try:
                            effect = _literal_keyword(node, "effect")
                        except ValueError:
                            pass
                        else:
                            if effect != "ui_state":
                                diagnostics.append(_diagnostic(
                                    "TASK_INTENT",
                                    "destination-only requests require effect='ui_state'",
                                    node,
                                ))
            continue
        if node.func.attr not in _SAFE_METHODS:
            diagnostics.append(_diagnostic("UNSAFE_METHOD", f"method {node.func.attr!r} is disallowed", node))

    if terminal_calls == 0:
        diagnostics.append(_diagnostic("TERMINAL_REQUIRED", "program must call ctx.finish or ctx.fail"))
    diagnostics.extend(_static_flow_diagnostics(tree))
    unique: list[MasterDiagnostic] = []
    seen: set[tuple[str, str, int]] = set()
    for item in diagnostics:
        key = (item.code, item.message, item.line)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _extract_source(content: Any) -> str:
    text = message_text(content).strip()
    fenced = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


def compile_master_program(
    *,
    llm: Any,
    system_prompt: str,
    task_context: dict[str, Any],
    max_attempts: int = 3,
    cache_system_prompt: bool = False,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> MasterProgram:
    """Generate and statically review one orchestration program."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    model_name = getattr(llm, "model_name", None) or getattr(llm, "model", None)
    generator = (
        llm.bind(max_tokens=12_000, **chat_request_kwargs(model_name))
        if callable(getattr(llm, "bind", None))
        else llm
    )
    rejected = ""
    last_diagnostics: list[MasterDiagnostic] = []
    for attempt in range(1, max_attempts + 1):
        payload = {"task": task_context}
        if rejected:
            payload["rejected_program"] = rejected
            payload["validation_issues"] = [item.render() for item in last_diagnostics]
        started_at = time.perf_counter()
        messages = [
            cacheable_system_message(
                system_prompt,
                enabled=cache_system_prompt,
            ),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        response = generator.invoke(messages)
        llm_elapsed_s = time.perf_counter() - started_at
        source = _extract_source(response.content)
        platform_context = task_context.get("platform")
        diagnostics = validate_master_source(
            source,
            platform_context=(
                platform_context if isinstance(platform_context, dict) else None
            ),
            user_goal=str(task_context.get("goal") or ""),
        )
        if on_event is not None:
            on_event(
                "master_compile_attempt",
                {
                    "attempt": attempt,
                    "source": source,
                    "diagnostics": [item.render() for item in diagnostics],
                    "llm_elapsed_s": round(llm_elapsed_s, 3),
                    "token_usage": response_usage(response),
                    "context_reports": diagnostic_prompt_reports(
                        "tool_agent.master",
                        messages,
                        response,
                        parsed={
                            "source": source,
                            "diagnostics": [item.render() for item in diagnostics],
                        },
                        schema="Reviewed Python program",
                    ),
                },
            )
        if not diagnostics:
            return MasterProgram(source=source, attempts=attempt)
        rejected = source
        last_diagnostics = diagnostics
    rendered = "; ".join(item.render() for item in last_diagnostics)
    raise MasterCompileError(f"Master program failed review after {max_attempts} attempts: {rendered}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkerOrchestrationContext:
    """Runtime API exposed to reviewed Master programs."""

    def __init__(
        self,
        *,
        data_store: RuntimeDataStore,
        run_gui_worker: Callable[[str, WorkerSpec], WorkerOutcome],
        trace: Callable[..., None],
    ) -> None:
        self._data_store = data_store
        self._run_gui_worker = run_gui_worker
        self._trace = trace
        self._records: dict[str, WorkerRecord] = {}
        self._transforms: dict[str, tuple[str, ResultRef]] = {}
        self._terminal: MasterTerminal | None = None

    @property
    def terminal(self) -> MasterTerminal | None:
        return self._terminal

    def _reuse(self, worker_id: str, signature: str) -> dict[str, Any] | None:
        record = self._records.get(worker_id)
        if record is None:
            return None
        if record.signature != signature:
            raise ValueError(
                f"worker_id {worker_id!r} is already bound to a different specification; "
                "use a new worker_id for a changed subgoal"
            )
        self._trace(
            "master_worker_reuse",
            worker_id=worker_id,
            kind="gui",
            outcome=record.outcome.model_dump(mode="json"),
        )
        return record.outcome.model_dump(mode="json")

    def gui_worker(
        self,
        *,
        worker_id: str,
        profile: Literal["operator", "collector"] | None = None,
        goal: str,
        success_criteria: list[str] | str,
        approach: str,
        input_refs: dict[str, str] | None = None,
        input_bindings: list[dict[str, Any]] | None = None,
        data_requirements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if _WORKER_ID_PATTERN.fullmatch(worker_id) is None:
            raise ValueError("worker_id must be a stable snake_case identifier")
        routed_inputs = dict(input_refs or {})
        for ref in routed_inputs.values():
            self._data_store.result_descriptor(ref)
        spec = WorkerSpec.model_validate(
            {
                "profile": profile,
                "goal": goal,
                "success_criteria": (
                    [success_criteria]
                    if isinstance(success_criteria, str)
                    else success_criteria
                ),
                "input_refs": routed_inputs,
                "input_bindings": input_bindings or [],
                "data_requirements": data_requirements or [],
                "strategy": {
                    "approach": approach,
                },
            }
        )
        signature = hashlib.sha256(_canonical(spec.model_dump(mode="json")).encode()).hexdigest()
        reused = self._reuse(worker_id, signature)
        if reused is not None:
            return reused
        self._trace(
            "master_worker_dispatch",
            worker_id=worker_id,
            kind="gui",
            goal=goal,
            spec=spec.model_dump(mode="json"),
        )
        try:
            outcome = self._run_gui_worker(worker_id, spec)
        except Exception as exc:  # noqa: BLE001 - a Worker failure is typed program data
            outcome = WorkerOutcome(
                phase="failed",
                summary=f"GUI Worker runtime error: {type(exc).__name__}: {exc}",
                steps=0,
            )
        self._records[worker_id] = WorkerRecord(worker_id, signature, outcome)
        self._trace(
            "master_worker_result",
            worker_id=worker_id,
            kind="gui",
            outcome=outcome.model_dump(mode="json"),
        )
        return outcome.model_dump(mode="json")

    def transform(
        self,
        *,
        transform_id: str,
        inputs: list[str],
        source: str,
        result_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if _WORKER_ID_PATTERN.fullmatch(transform_id) is None:
            raise ValueError("transform_id must be a stable snake_case identifier")
        if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
            raise ValueError("transform inputs must be a list of data refs")
        Draft202012Validator.check_schema(result_schema)
        validate_transform_source(source)
        request = {"inputs": inputs, "source": source, "result_schema": result_schema}
        signature = hashlib.sha256(_canonical(request).encode()).hexdigest()
        prior = self._transforms.get(transform_id)
        if prior is not None:
            prior_signature, prior_descriptor = prior
            if prior_signature != signature:
                raise ValueError(
                    f"transform_id {transform_id!r} is already bound to a different specification"
                )
            self._trace(
                "transform_reused",
                transform_id=transform_id,
                result_ref=prior_descriptor.model_dump(mode="json"),
            )
            return prior_descriptor.model_dump(mode="json")
        row_schemas = [
            schema for ref in inputs
            if (schema := self._data_store.ref_row_schema(ref)) is not None
        ]
        if row_schemas:
            combined_row_schema = {
                "type": "object",
                "properties": {
                    key: value
                    for schema in row_schemas
                    for key, value in (schema.get("properties") or {}).items()
                },
            }
            validate_transform_row_fields(source, combined_row_schema)
        self._trace(
            "transform_started",
            transform_id=transform_id,
            inputs=inputs,
            source=source,
            result_schema=result_schema,
        )
        try:
            input_values = [self._data_store.resolve_value(item) for item in inputs]
            value = execute_transform(source, input_values, result_schema)
            descriptor = self._data_store.put_result(
                value,
                result_schema,
                summary=f"Deterministic transform {transform_id} completed.",
                source_refs=inputs,
            )
        except Exception as exc:
            self._trace(
                "transform_failed",
                transform_id=transform_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._trace(
            "transform_completed",
            transform_id=transform_id,
            inputs=inputs,
            result_ref=descriptor.model_dump(mode="json"),
        )
        self._transforms[transform_id] = (signature, descriptor)
        return descriptor.model_dump(mode="json")

    def worker_result(self, worker_id: str) -> dict[str, Any] | None:
        record = self._records.get(worker_id)
        return record.outcome.model_dump(mode="json") if record is not None else None

    def finish(
        self,
        result_ref: str,
        *,
        effect: Literal["mutation", "data", "ui_state"],
    ) -> None:
        if not isinstance(result_ref, str):
            raise ValueError(
                "finish requires a ref string such as result['ref'] or "
                "outcome['collection_ref']['ref'], "
                f"got {type(result_ref).__name__}"
            )
        try:
            descriptor = self._data_store.result_descriptor(result_ref)
        except KeyError as result_error:
            try:
                collection = self._data_store.collection_descriptor(result_ref)
            except KeyError:
                raise result_error
            if effect != "data":
                raise ValueError(
                    "finish accepts a CollectionRef only with effect='data'"
                )
            descriptor = self._data_store.put_result(
                self._data_store.collection_rows(collection.ref),
                {"type": "array", "items": collection.row_schema},
                summary=(
                    f"Collected {collection.row_count} row(s) for "
                    f"{collection.requirement_id}."
                ),
                source_refs=[collection.ref],
            )
        if effect == "data" and not self._data_store.is_data_result(descriptor.ref):
            raise ValueError(
                "effect='data' requires a ResultRef derived from collected data"
            )
        self._terminal = MasterTerminal(
            phase="completed",
            summary=descriptor.summary or "Coding Master accepted the ResultRef.",
            result_ref=descriptor.ref,
            effect=effect,
        )
        raise _ProgramHalt

    def fail(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("fail requires a concrete reason")
        summary = reason.strip()
        latest = next(reversed(self._records.values()), None)
        detail = (
            latest.outcome.summary.strip()
            if latest is not None and latest.outcome.phase == "failed"
            else ""
        )
        if detail and detail.casefold() not in summary.casefold():
            summary = f"{summary.rstrip('.')} — {detail}"
        self._terminal = MasterTerminal(phase="failed", summary=summary)
        raise _ProgramHalt

    def reset_terminal(self) -> None:
        self._terminal = None


def execute_master_program(
    source: str,
    ctx: WorkerOrchestrationContext,
    *,
    max_lines: int = 10_000,
) -> MasterExecution:
    """Execute one reviewed orchestration program with a bounded line budget."""
    diagnostics = validate_master_source(source)
    if diagnostics:
        return MasterExecution(error="; ".join(item.render() for item in diagnostics))
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    ctx.reset_terminal()
    try:
        exec(compile(source, _MASTER_FILENAME, "exec"), namespace, namespace)
        lines = 0

        def tracer(frame: Any, event: str, arg: Any) -> Any:
            del arg
            nonlocal lines
            if event == "line" and frame.f_code.co_filename == _MASTER_FILENAME:
                lines += 1
                if lines > max_lines:
                    raise RuntimeError(f"Master program exceeded {max_lines} executed lines")
            return tracer

        previous_trace = sys.gettrace()
        sys.settrace(tracer)
        try:
            namespace["run"](ctx)
        except _ProgramHalt:
            pass
        finally:
            sys.settrace(previous_trace)
        if ctx.terminal is None:
            return MasterExecution(
                error="Master program returned without a terminal ctx call"
            )
        return MasterExecution(terminal=ctx.terminal)
    except BaseException as exc:  # noqa: BLE001 - sandbox boundary reports errors as data
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return MasterExecution(error=f"{type(exc).__name__}: {exc}")


__all__ = [
    "MasterCompileError",
    "MasterDiagnostic",
    "MasterExecution",
    "MasterProgram",
    "MasterTerminal",
    "WorkerOrchestrationContext",
    "compile_master_program",
    "execute_master_program",
    "validate_master_source",
]
