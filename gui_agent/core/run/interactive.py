"""Interactive statement executor adapter.

The DSL interpreter yields an interactive ``Run``.  This module translates that statement into
the Milestone representation consumed by the supervisor, starts the Milestone loop, and reads
declared values from its terminal observation.  Retry and re-planning policy live elsewhere.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Callable, Literal

from gui_agent.core.orchestrator.contracts import normalize_return_reads
from gui_agent.core.orchestrator.program import INTERACTIVE_KINDS, Run, RunLike
from gui_agent.core.schemas import Milestone
from gui_agent.core.run.execution_signals import ExecutionContract

if TYPE_CHECKING:
    from gui_agent.core.schemas import Observation


_KIND_MAP: dict[str, tuple[str, str]] = {
    "navigation": ("navigation", "visible_once"),
    "filter": ("filter", "visible_once"),
    "action": ("action", "visible_once"),
}


def _milestone_id(run: Run, index: int) -> str:
    base = run.var or f"m{index}_{run.kind}"
    if run.var and run.kind in INTERACTIVE_KINDS and run.returns:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", run.name).strip("_")[:32]
        digest = hashlib.sha1(run.name.encode("utf-8")).hexdigest()[:8]
        return f"{base}_{slug or digest}_{digest}"
    return base


def milestone_for_run(run: Run, index: int) -> Milestone:
    """Translate one interactive DSL statement into the Milestone executor's input."""
    if run.is_query:
        raise ValueError(
            f"query run（kind={run.kind}）不是 milestone：非交互原语由解释器驱动，"
            "需要交互时应先升格为 navigation/filter/action。"
        )
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    description = run.name
    if run.returns:
        description = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    return Milestone(
        id=_milestone_id(run, index),
        name=run.name,
        description=description,
        success_condition=run.success_condition or f"完成「{run.name}」",
        kind=kind,  # type: ignore[arg-type]
        completion_strategy=strategy,  # type: ignore[arg-type]
        precondition=run.precondition,
        require_fresh_action=run.kind == "action",
        returns=list(run.returns),
        read_spec=run.read_spec or "",
    )


def task_type_for_run(run: RunLike) -> Literal["action", "analysis"]:
    """Choose the supervisor read mode for a statement."""
    return "analysis" if run.kind in {"read", "data_query"} else "action"


def start_milestone(supervisor, run: Run, index: int, *, fresh_advance: bool = False) -> Milestone:
    """Seed the supervisor with the Milestone for one interactive statement."""
    milestone = milestone_for_run(run, index)
    set_contract = getattr(supervisor, "set_execution_contract", None)
    if callable(set_contract):
        set_contract(ExecutionContract.from_milestone(milestone))
    supervisor.reseed(
        milestone,
        task_type=task_type_for_run(run),
        fresh_advance=fresh_advance,
    )
    return milestone


def extract_run_returns(
    run: Run,
    observation: "Observation",
    *,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    say: Callable[[str], None] = lambda _s: None,
) -> dict[str, str]:
    """Read declared outputs from an interactive statement's terminal observation.

    Source priority is deterministic URL JSON, DOM form controls, then visual structured read.
    """
    if run is None or not run.returns or run.is_query:
        return {}

    from gui_agent.core.orchestrator.primitives.url_json_read import read_json_url_returns

    returns = list(run.returns)
    read_spec = run.read_spec or ""
    json_reads = read_json_url_returns(run.name or "", returns, read_spec)
    if json_reads is not None and any(str(json_reads.get(field, "")).strip() for field in returns):
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
