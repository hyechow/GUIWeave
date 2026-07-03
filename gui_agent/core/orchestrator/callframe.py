"""Milestone-as-function calling convention — the ABI between the DSL program and the executor.

A milestone is a FUNCTION the program calls; this module owns the call boundary:

- 入参   = 入口状态（Run.from_state，FROM）+ 目标规格（name / success_condition / read_spec）
- 出参   = Run.returns —— 声明的返回字段，验收时必须读到非空值（合同，不是提示）
- 后置条件 = success_condition（TO 状态），由执行器的 checker 判定
- 纯度   = read / data_query 是纯查询；navigation / filter / action 是命令
- 异常   = infeasible（kickback directive → 重编排）/ 返回值空缺（有界恢复 → 诚实失败）

调用方（DSL 解释器）与被调用方（milestone supervisor）互相不知道对方存在；agent loop
（core/run/loop.py）保留 turn/帧 的控制流，但每一个边界决策都从这里取：

- ``open_call()``                    —— 调用：把 Run marshal 进执行器（supervisor.reseed）
- ``extract_ui_returns()``           —— 返回：从完成帧读取声明的返回字段（URL JSON → DOM → 视觉）
- ``missing_ui_return_fields()``     —— 返回值合同检查（缺失 = 违约，不得带空值推进）
- ``ReturnRecoveryLedger`` + ``tighten_ui_return_run()`` —— 违约的有界恢复（收紧合同重试）
- ``force_interactive_return_recovery()`` —— 空返回 kickback 后对新程序的入口修复
- ``should_kickback_replan()``       —— infeasible 异常门（是否允许向上抛给重编排）

Marshalling 本身（Run→Milestone / RunResult 打包）住在 engine.py；本模块是调用协议。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from gui_agent.core.orchestrator.engine import task_type_for, to_milestone

if TYPE_CHECKING:
    from gui_agent.core.schemas import Milestone, Observation

# 返回字段为空时，最多把当前 UI run 收紧后重新驱动几次
MAX_EMPTY_RETURN_RECOVERIES = 3

# Feasibility Guard: how many times a single run may re-decompose after a feasibility kick-back. Bounded
# to avoid an infinite re-plan loop (the same dead-end milestone re-appearing). One is enough to
# swap an infeasible route for the prescribed feasible one; a second kick-back ends the run.
MAX_KICKBACK_REPLANS = 1


_EMPTY_RETURN_OK_CUES = (
    "留空",
    "未选中",
    "selectedindex=-1",
    "unselected",
    "no selection",
    "empty allowed",
    "allow empty",
)


def _compact_text(text: str) -> str:
    return "".join(ch.lower() for ch in str(text or "") if not ch.isspace())


def _read_spec_fragments(text: str) -> list[str]:
    spec = str(text or "")
    for sep in ("；", ";", "\n", "。"):
        spec = spec.replace(sep, "\n")
    return [frag.strip() for frag in spec.splitlines() if frag.strip()]


def ui_return_field_allows_empty(run: object, field: str) -> bool:
    """Whether this return field explicitly treats blank as a valid value."""
    field_key = _compact_text(field)
    if not field_key:
        return False
    for fragment in _read_spec_fragments(getattr(run, "read_spec", "") or ""):
        compact = _compact_text(fragment)
        if field_key not in compact:
            continue
        if any(_compact_text(cue) in compact for cue in _EMPTY_RETURN_OK_CUES):
            return True
    return False


def missing_ui_return_fields(run: object, reads: dict[str, str]) -> list[str]:
    """Return UI-run fields that were declared but not actually read.

    A navigation/action/filter run with ``returns`` is only complete for the
    orchestrator once those fields have values. Empty values mean the milestone
    was accepted too early or on the wrong page, so the plan should not advance
    to later steps that interpolate blanks.
    """
    if run is None or not getattr(run, "returns", None):
        return []
    if getattr(run, "kind", "") in {"read", "data_query"}:
        return []
    missing: list[str] = []
    for field in getattr(run, "returns", []):
        field_name = str(field)
        if str(reads.get(field_name, "")).strip():
            continue
        if field_name in reads and ui_return_field_allows_empty(run, field_name):
            continue
        missing.append(field_name)
    return missing


def tighten_ui_return_run(run: object, missing: list[str], reads: dict[str, str], *, attempt: int) -> object:
    """Make a returning UI run stricter after its completion frame read blanks.

    The decomposer may author a broad success condition such as "page loaded" while
    the run also declares return fields. If the checker accepts the page before
    those fields are visible, continue the same UI milestone with an explicit
    non-empty return-field gate instead of advancing with blanks or waiting as if
    the page were loading.
    """
    if run is None or not hasattr(run, "model_copy"):
        return run
    returns = [str(field) for field in getattr(run, "returns", [])]
    missing_text = "、".join(str(field) for field in missing)
    present = {
        str(field): str(value).strip()
        for field, value in reads.items()
        if str(value).strip()
    }
    present_text = "、".join(f"{field}={value}" for field, value in present.items()) or "无"
    base_success = str(getattr(run, "success_condition", "") or f"完成「{getattr(run, 'name', '当前子目标')}」")
    base_read_spec = str(getattr(run, "read_spec", "") or "")
    recovery = (
        f"返回字段恢复尝试 {attempt}: 当前完成帧未读到所有必需字段。"
        f"已读非空值：{present_text}；缺失字段：{missing_text}。"
        f"只有当这些字段都能从界面明确读取到非空值时才算完成：{'、'.join(returns)}。"
        "如果当前屏幕不可见，不要验收完成；继续执行必要的页面内操作，例如等待、滚动、"
        "打开可见的详情/统计/菜单入口、或使用页面搜索，直到缺失字段的具体值可见。"
    )
    name = str(getattr(run, "name", "当前子目标"))
    return run.model_copy(update={
        "name": f"{name}（继续定位返回字段：{missing_text}）",
        "success_condition": f"{base_success}\n{recovery}",
        "read_spec": f"{base_read_spec}\n{recovery}".strip(),
    })


def force_interactive_return_recovery(program: object, directive: str) -> object:
    """Convert a mistaken current-frame read into a UI locating run after empty returns.

    A kickback caused by empty UI return fields means the current frame did not
    expose the required values. If the redecomposer responds with a scalar
    ``read`` as the first step, that read can only repeat the same empty frame.
    Treat it as an interactive page-location milestone so the supervisor can
    scroll, expand sections, or navigate within the page before the structured
    return extraction runs.
    """
    if "实际读取结果为空" not in directive or "返回字段" not in directive:
        return program
    if not hasattr(program, "statements") or not hasattr(program, "model_copy"):
        return program

    from gui_agent.core.orchestrator.program import Run

    statements = list(getattr(program, "statements", []) or [])
    if not statements:
        return program
    first = statements[0]
    if (
        not isinstance(first, Run)
        or first.kind != "read"
        or not first.returns
    ):
        return program

    fields = "、".join(str(field) for field in first.returns)
    recovery = (
        "上一次已在当前完成帧尝试读取这些返回字段但结果为空。"
        f"本步必须先通过界面定位让字段值可见，字段包括：{fields}。"
        "如果当前屏幕看不到这些值，不要验收完成；继续滚动、展开页面内相关区域、"
        "打开可见的统计/详情入口或使用页面搜索，直到所有字段都有非空可读值。"
    )
    success = str(first.success_condition or f"页面显示可读取的返回字段：{fields}")
    read_spec = str(first.read_spec or "")
    statements[0] = first.model_copy(update={
        "kind": "navigation",
        "success_condition": f"{success}\n{recovery}",
        "read_spec": f"{read_spec}\n{recovery}".strip(),
    })
    return program.model_copy(update={"statements": statements})


def should_kickback_replan(sv_step, program, redecompose, replan_count: int) -> bool:
    """Decide whether a stop step is a Feasibility Guard kick-back to re-decompose (vs a terminal stop).

    True only when: we're in orchestrator mode (program), the supervisor attached a re-plan
    directive (milestone judged infeasible), a redecompose callable is wired, and the per-run
    budget is not yet spent. Otherwise the stop is handled normally (terminal)."""
    return bool(
        program is not None
        and getattr(sv_step, "replan_directive", None)
        and callable(redecompose)
        and replan_count < MAX_KICKBACK_REPLANS
    )


class ReturnRecoveryLedger:
    """Bounded retry budget for the empty-returns contract violation, keyed per call site.

    每个调用点（run_index + var/name + returns 合同）独立计数：同一个 run 收紧重试最多
    ``max_attempts`` 次，之后 ``next_attempt`` 返回 None —— 调用方必须打包 completed=False
    的诚实失败，而不是带着空值推进。"""

    def __init__(self, max_attempts: int = MAX_EMPTY_RETURN_RECOVERIES):
        self.max_attempts = max_attempts
        self._attempts: dict[tuple[int, str, tuple[str, ...]], int] = {}

    @staticmethod
    def _key(run_index: int, run: object) -> tuple[int, str, tuple[str, ...]]:
        return (
            run_index,
            str(getattr(run, "var", "") or getattr(run, "name", "")),
            tuple(str(field) for field in getattr(run, "returns", [])),
        )

    def next_attempt(self, run_index: int, run: object) -> Optional[int]:
        """Consume one retry; returns the attempt number, or None when the budget is spent."""
        key = self._key(run_index, run)
        attempt = self._attempts.get(key, 0) + 1
        if attempt > self.max_attempts:
            return None
        self._attempts[key] = attempt
        return attempt


def open_call(supervisor, run, index: int, *, fresh_advance: bool = False) -> "Milestone":
    """The CALL op: marshal one Run into the executor and point the supervisor at it.

    Single-sourcing the reseed keeps every dispatch site (initial seed, hand-off advance,
    tighten-retry) on the same convention: milestone identity from ``to_milestone``, read
    gate from ``task_type_for``. Returns the marshalled Milestone (callers record it)."""
    milestone = to_milestone(run, index)
    supervisor.reseed(milestone, task_type=task_type_for(run), fresh_advance=fresh_advance)
    return milestone


def extract_ui_returns(
    run,
    observation: "Observation",
    *,
    check_knowledge: str = "",
    prepare_vision_prompt_png=None,
    say: Callable[[str], None] = lambda _s: None,
) -> dict[str, str]:
    """The RETURN op: extract a UI run's declared return fields from its completion frame.

    Source priority: URL JSON（确定性）→ DOM form controls（native select 权威，live 185）
    → 视觉 structured_read 兜底，只补 DOM 未命中的字段。read/data_query 不走这里（它们
    是非 UI 纯查询，由 non_interactive 驱动）。"""
    if run is None or not getattr(run, "returns", None):
        return {}
    if getattr(run, "kind", "") in {"read", "data_query"}:
        return {}
    from gui_agent.core.orchestrator.url_json_read import read_json_url_returns

    returns = list(run.returns)
    read_spec = getattr(run, "read_spec", "") or ""
    json_reads = read_json_url_returns(getattr(run, "name", "") or "", returns, read_spec)
    if json_reads is not None and any(str(json_reads.get(field, "")).strip() for field in returns):
        say(f"  [Orchestrator] URL JSON 返回读取 {returns} → {json_reads}")
        return json_reads
    from gui_agent.core.orchestrator.structured_read import (
        read_form_control_returns,
        structured_read,
    )

    # DOM-first: a native <select>'s selected value is authoritative over a vision guess
    # (live 185: vision read the first LISTED option Burlap instead of the selected Cotton;
    # the DOM form control carries the real selection). An unselected native select is also
    # authoritative empty, not "missing"; vision fills only fields the DOM did not match.
    dom_reads = read_form_control_returns(getattr(observation, "form_controls", None), returns)
    missing = [f for f in returns if f not in dom_reads]
    if not missing:
        say(f"  [Orchestrator] DOM 表单返回读取 {returns} → {dom_reads}")
        return dom_reads
    reads = structured_read(
        observation.png_bytes,
        missing,
        read_spec=read_spec,
        check_knowledge=check_knowledge,
        prepare_vision_prompt_png=prepare_vision_prompt_png,
    )
    merged = {**reads, **dom_reads}
    if dom_reads:
        say(f"  [Orchestrator] 返回读取 {returns} → DOM {dom_reads} + 视觉 {reads}")
    else:
        say(f"  [Orchestrator] 动作返回读取 {returns} → {reads}")
    return merged
