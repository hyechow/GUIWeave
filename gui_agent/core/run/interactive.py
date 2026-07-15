"""Interactive statement executor adapter.

Translates a DSL interactive ``Run`` into a frozen ``StatementContract``, begins
the supervisor statement runtime, and reads declared returns from the terminal
observation. Retry and re-planning policy live elsewhere.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable, Literal

from gui_agent.core.orchestrator.contracts import normalize_return_reads
from gui_agent.core.orchestrator.program import INTERACTIVE_KINDS, Query, Read, Run, RunLike
from gui_agent.core.run.execution_signals import action_requires_mutation_evidence
from gui_agent.core.schemas import StatementContract, StatementInfo

if TYPE_CHECKING:
    from gui_agent.core.schemas import Observation


_KIND_MAP: dict[str, tuple[str, str]] = {
    "navigation": ("navigation", "visible_once"),
    "filter": ("filter", "visible_once"),
    "action": ("action", "visible_once"),
}

_VALUE_CONVERGE_CONTROLS = ("picker", "滚轮", "选择器", "步进器", "滑块", "spinner")
_VALUE_SET_WORDS = ("设置为", "设为", "调到", "调整为", "改为", "选到", "显示为", "设定为")
_VALUE_DOMAINS = (
    "时间", "日期", "闹钟", "小时", "分钟", "上午", "下午", "am", "pm",
    "数量", "数值", "音量", "亮度", "比例", "百分比", "档", "级", "年", "月", "日",
)


def _needs_value_convergence(run: Run) -> bool:
    text = f"{run.name}\n{run.success_condition}".casefold()
    if any(word in text for word in _VALUE_CONVERGE_CONTROLS):
        return any(word in text for word in _VALUE_SET_WORDS)
    return (
        any(word in text for word in _VALUE_SET_WORDS)
        and any(word in text for word in _VALUE_DOMAINS)
        and bool(re.search(r"\d|am|pm|上午|下午|%", text))
    )


def statement_id_for_run(run: RunLike, program_index: int) -> str:
    """Stable Program-statement id shared by interactive and immediate paths.

    The stable identity is the compile-time `statement_id` assigned at decompose time
    (``assign_statement_ids``) — preserved across return-tighten (model_copy) and across
    foreach/function re-yields. The fallback (var / m{index}_{kind}) is only for Runs built
    outside the decomposer (inline/test) and is NOT tighten-stable.
    """
    sid = getattr(run, "statement_id", "") or ""
    if sid:
        return sid
    return run.var or f"m{program_index}_{run.kind}"


def contract_for_run(run: Run, program_index: int) -> StatementContract:
    """Translate one interactive DSL statement into a frozen execution contract."""
    if run.is_query:
        raise ValueError(
            f"query run（kind={run.kind}）不是 interactive contract："
            "非交互原语由解释器驱动，需要交互时应先升格为 navigation/filter/action。"
        )
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    if run.kind in {"action", "filter"} and _needs_value_convergence(run):
        strategy = "repeat_until_satisfied"
    description = run.name
    if run.returns:
        description = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    mutation_evidence = bool(
        run.kind == "action"
        and action_requires_mutation_evidence(
            effect_mode=run.effect_mode,
            target_values=run.target_values,
            persistence=run.persistence,
            output_fields=run.returns,
        )
    )
    return StatementContract(
        id=statement_id_for_run(run, program_index),
        name=run.name,
        description=description,
        success_condition=run.success_condition or f"完成「{run.name}」",
        kind=kind,  # type: ignore[arg-type]
        completion_strategy=strategy,  # type: ignore[arg-type]
        precondition=run.precondition,
        effect_mode=run.effect_mode if mutation_evidence else None,
        persistence=run.persistence,
        target_controls=list(run.target_controls),
        target_values=dict(run.target_values),
        returns=list(run.returns),
        read_spec=run.read_spec or "",
    )


def statement_info_for_run(run: RunLike, program_index: int) -> StatementInfo:
    """Persisted contract DTO for the first turn of a statement invocation."""
    sid = statement_id_for_run(run, program_index)
    if isinstance(run, Run) and run.is_interactive:
        return statement_info_from_contract(contract_for_run(run, program_index))
    sql = str(getattr(run, "sql", "") or "")
    data_scope = str(getattr(run, "data_scope", "") or "")
    returns = list(getattr(run, "returns", None) or [])
    return StatementInfo(
        id=sid,
        name=run.name,
        description=run.name,
        kind=str(run.kind),
        success_condition=str(getattr(run, "success_condition", "") or run.name),
        returns=returns,
        read_spec=str(getattr(run, "read_spec", "") or ""),
        sql=sql,
        data_scope=data_scope,
    )


def statement_info_from_contract(contract: StatementContract) -> StatementInfo:
    return StatementInfo(
        id=contract.id,
        name=contract.name,
        description=contract.description,
        kind=contract.kind,
        success_condition=contract.success_condition,
        completion_strategy=contract.completion_strategy,
        precondition=contract.precondition,
        effect_mode=contract.effect_mode,
        persistence=contract.persistence,
        target_controls=list(contract.target_controls),
        target_values=dict(contract.target_values),
        returns=list(contract.returns),
        read_spec=contract.read_spec or "",
    )


def task_type_for_run(run: RunLike) -> Literal["action", "analysis"]:
    """Choose the supervisor read mode for a statement."""
    return "analysis" if run.kind in {"read", "data_query"} else "action"


def start_statement(
    supervisor,
    run: Run,
    index: int,
    *,
    fresh_advance: bool = False,
    instance_id: str = "",
) -> StatementContract:
    """Compile and begin one interactive statement."""
    contract = contract_for_run(run, index)
    iid = instance_id or f"inst-{index}-{contract.id}"
    supervisor.begin_statement(
        contract,
        instance_id=iid,
        task_type=task_type_for_run(run),
        fresh_advance=fresh_advance,
    )
    return contract


def extract_run_returns(
    run: Run,
    observation: "Observation",
    *,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    say: Callable[[str], None] = lambda _s: None,
) -> dict[str, str]:
    """Read declared outputs from an interactive statement's terminal observation."""
    if run is None or not run.returns or run.is_query:
        return {}

    from gui_agent.core.orchestrator.primitives.url_json_read import read_json_url_returns

    returns = list(run.returns)
    read_spec = run.read_spec or ""
    json_reads = read_json_url_returns(run.name or "", returns, read_spec)
    if json_reads is not None and any(
        str(json_reads.get(field, "")).strip() for field in returns
    ):
        json_reads = normalize_return_reads(run, json_reads)
        say(f"  [Orchestrator] URL JSON 返回读取 {returns} → {json_reads}")
        return json_reads

    from gui_agent.core.orchestrator.primitives.structured_read import (
        read_form_control_returns,
        structured_read,
    )

    dom_reads = read_form_control_returns(
        getattr(observation, "form_controls", None),
        returns,
        read_spec=read_spec,
    )
    missing = [field for field in returns if field not in dom_reads]
    if not missing:
        dom_reads = normalize_return_reads(run, dom_reads)
        say(f"  [Orchestrator] DOM 表单返回读取 {returns} → {dom_reads}")
        return dom_reads

    reads = structured_read(
        observation.png_bytes,
        missing,
        read_spec=read_spec,
        check_knowledge=check_knowledge,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
    )
    merged = normalize_return_reads(run, {**reads, **dom_reads})
    if dom_reads:
        say(f"  [Orchestrator] 返回读取 {returns} → DOM {dom_reads} + 视觉 {reads}")
    else:
        say(f"  [Orchestrator] 动作返回读取 {returns} → {reads}")
    return merged
