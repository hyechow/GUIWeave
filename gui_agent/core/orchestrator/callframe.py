"""Milestone-as-function calling convention — the ABI between the DSL program and the executor.

A milestone is a FUNCTION the program calls; this module owns the call boundary:

- 入参   = 入口状态（Run.from_state，FROM）+ 目标规格（name / success_condition / read_spec）
- 出参   = Run.returns —— 声明的返回字段，验收时必须读到非空值（合同，不是提示）
- 后置条件 = success_condition（TO 状态），由执行器的 checker 判定
- 执行模式 = read / data_query 是非交互纯查询（解释器确定性执行）；navigation / filter /
  action 是交互调用（跨进非确定性 GUI 世界的 FFI，本模块的合同全部压在这道边界上）
- 异常   = infeasible（kickback directive → 重编排）/ 返回值空缺（有界恢复 → 诚实失败）

调用方（DSL 解释器）与被调用方（milestone supervisor）互相不知道对方存在；agent loop
（core/run/loop.py）保留 turn/帧 的控制流，但每一个边界决策都从这里取：

- ``to_milestone()`` / ``task_type_for()`` / ``package_result()`` —— marshalling：交互 action
  ⇄ 执行器的 Milestone 载体格式（call 的入栈/返回打包，即 FFI 的调用/返回约定本身）
- ``open_call()``                    —— 调用：把 Run marshal 进执行器（supervisor.reseed）
- ``extract_ui_returns()``           —— 返回：从完成帧读取声明的返回字段（URL JSON → DOM → 视觉）
- ``missing_ui_return_fields()``     —— 返回值合同检查（缺失 = 违约，不得带空值推进）
- ``ReturnRecoveryLedger`` + ``tighten_ui_return_run()`` —— 违约的有界恢复（收紧合同重试）
- ``force_interactive_return_recovery()`` —— 空返回 kickback 后对新程序的入口修复
- ``should_kickback_replan()``       —— infeasible 异常门（是否允许向上抛给重编排）

编译期的 AST normalize passes（dispatch gate / precondition / chain_from_states 等）住在
passes.py（编译 middle-end），不在这里——本模块只管 FFI 边界。retired: engine.py 已拆入这两处。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field as dc_field
from typing import TYPE_CHECKING, Callable, Literal, Sequence

from gui_agent.core.schemas import Milestone

from .program import INTERACTIVE_KINDS, Run, RunLike
from .runner import RunResult

# The empty-return retry budget + its ledger moved to recovery.py (exception-system Stage A: the
# task-wide RecoveryLedger subsumes ReturnRecoveryLedger); explicit re-exports keep the callframe
# ABI surface stable for existing importers.
from .recovery import (
    MAX_EMPTY_RETURN_RECOVERIES as MAX_EMPTY_RETURN_RECOVERIES,
    ReturnRecoveryLedger as ReturnRecoveryLedger,
)

if TYPE_CHECKING:
    from gui_agent.core.schemas import Observation


# ── marshalling: Run → Milestone (the FFI call ABI) ──────────────────────────────────
# Marshalling into the executor's Milestone carrier format IS the FFI boundary, so it lives
# here with the rest of the call convention (moved from the retired engine.py). Only an
# INTERACTIVE action becomes a Milestone; a query (read/data_query) is a non-interactive
# statement driven by drive_pending_non_ui — marshalling one is a type error.

# Interactive Run kind -> executor (kind, completion_strategy). Query kinds have no rows here:
# to_milestone rejects them before lookup (queries never marshal into the executor).
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


def to_milestone(run: Run, index: int) -> Milestone:
    """Marshal one interactive action into a Milestone the executor can drive.

    `returns`/`read_spec` travel BOTH ways: structurally (Milestone.returns/read_spec — the
    出参合同 channel a consumer can read without parsing prose) AND folded into the description
    (so existing read-instruction prompts keep targeting them unchanged).

    BOUNDARY: only interactive actions become milestones. A query (read/data_query) is a
    non-interactive program-layer statement driven by drive_pending_non_ui — marshalling one into
    the executor is a type error, not a fallback (a read that needs interaction must first be
    PROMOTED to an interactive action, see force_interactive_return_recovery)."""
    if run.is_query:
        raise ValueError(
            f"query run（kind={run.kind}）不是 milestone：非交互原语由 drive_pending_non_ui 驱动，"
            "需要交互时应先升格为命令（kind=navigation）再 marshal。"
        )
    kind, strategy = _KIND_MAP.get(run.kind, ("action", "visible_once"))
    desc = run.name
    if run.returns:
        desc = f"{run.name}（读取字段：{'、'.join(run.returns)}）"
    success = run.success_condition or f"完成「{run.name}」"
    return Milestone(
        id=_milestone_id(run, index),
        name=run.name,
        description=desc,
        success_condition=success,
        kind=kind,  # type: ignore[arg-type]  # validated against MilestoneKind Literal
        completion_strategy=strategy,  # type: ignore[arg-type]
        precondition=run.precondition,  # entry-state gate: checker judges frame-1, no fresh-nav skip
        require_fresh_action=run.kind == "action",
        returns=list(run.returns),
        read_spec=run.read_spec or "",
    )


def task_type_for(run: RunLike) -> Literal["action", "analysis"]:
    """A read/query -> 'analysis' so the supervisor's task_type-gated reader actually reads
    (the executor gates reading on task_type; see policy._ctx / _default_read_instruction)."""
    return "analysis" if run.kind in {"read", "data_query"} else "action"


def package_result(
    run: RunLike, *, completed: bool, summary: str, notes: list[str],
    reads: dict[str, str] | None = None,
    rows: list[dict[str, str]] | None = None,
) -> RunResult:
    """Package a finished statement's loop state into the RunResult contract. `reads` is the
    structured {field: value} for a scalar read; `rows` is the LIST form for a foreach-accumulated
    table (one dict per row); other statements pass none."""
    return RunResult(
        completed=completed,
        failed=not completed,
        reads=dict(reads) if reads else {},
        rows=list(rows) if rows else [],
        summary=summary,
        evidence=list(notes),
    )

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


def _is_query_run(run: object) -> bool:
    """Non-interactive pure query (read/data_query)? Reads the RunLike.is_query property every IR
    node carries; the getattr default keeps the boundary duck-safe for the `object`-typed callers
    (a None/foreign value is treated as not-a-query)."""
    return bool(getattr(run, "is_query", False))


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
    if _is_query_run(run):
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


# ── 返回值域校验（typed returns）─────────────────────────────────────────────────
# 出参不只是「非空」，还要「在取值域内」：读到形似垃圾的值（URL 字段读到一句散文、计数字段
# 读到无数字文本、枚举字段读到域外值）= 合同违约，走与空值相同的有界恢复，而不是静默给错答。
# 域来源优先级：Run.return_domains 显式声明（decomposer 授权）> 字段名线索的保守推断。

_DOMAIN_URL_NAME_RE = re.compile(r"url|href|链接", re.IGNORECASE)
_DOMAIN_NUMBER_NAME_RE = re.compile(r"count|total|amount|price|数量|计数|金额|条数|行数|总数|数目", re.IGNORECASE)
_DOMAIN_DATE_NAME_RE = re.compile(r"\bdate\b|\btime\b|日期|时间", re.IGNORECASE)
_HAS_DIGIT_RE = re.compile(r"\d")


def _value_fits_domain(value: str, domain: str) -> tuple[bool, str]:
    """(fits, reason-if-not). Domain grammar: url | number | date | enum:a|b|c | text."""
    text = str(value).strip()
    kind = domain.strip().lower()
    if kind.startswith("enum:"):
        options = [opt.strip() for opt in domain.split(":", 1)[1].split("|") if opt.strip()]
        normalized = {opt.casefold() for opt in options}
        if text.casefold() in normalized:
            return True, ""
        return False, f"不在枚举域 {options} 内"
    if kind == "url":
        if " " not in text and ("://" in text or "/" in text):
            return True, ""
        return False, "不是 URL/路径形态"
    if kind == "number":
        if _HAS_DIGIT_RE.search(text):
            return True, ""
        return False, "不含任何数字，不是数值/计数"
    if kind == "date":
        if _HAS_DIGIT_RE.search(text):
            return True, ""
        return False, "不含任何数字，不是日期/时间"
    return True, ""  # text / 未知域 = 不约束


def _inferred_domain(field_name: str) -> str:
    """Conservative field-name-cue inference, used only when no explicit domain is declared."""
    if _DOMAIN_URL_NAME_RE.search(field_name):
        return "url"
    if _DOMAIN_NUMBER_NAME_RE.search(field_name):
        return "number"
    if _DOMAIN_DATE_NAME_RE.search(field_name):
        return "date"
    return ""


@dataclass
class DomainViolation:
    """One non-empty return value that fell outside its declared/inferred domain."""

    field: str
    value: str
    domain: str
    reason: str

    def describe(self) -> str:
        return f"字段「{self.field}」读到「{self.value}」：{self.reason}（要求域 {self.domain}）"


def out_of_domain_return_fields(run: object, reads: dict[str, str]) -> list[DomainViolation]:
    """Check every NON-empty declared return value against its domain.

    Empty values are the missing-check's business (``missing_ui_return_fields``); this only
    rejects values that were read but are the WRONG SHAPE — the "抓垃圾静默给错答" class."""
    if run is None or not getattr(run, "returns", None):
        return []
    if _is_query_run(run):
        return []
    declared: dict[str, str] = {
        str(k): str(v) for k, v in (getattr(run, "return_domains", None) or {}).items()
    }
    violations: list[DomainViolation] = []
    for field_name in (str(f) for f in getattr(run, "returns", [])):
        value = str(reads.get(field_name, "")).strip()
        if not value:
            continue
        domain = declared.get(field_name) or _inferred_domain(field_name)
        if not domain:
            continue
        fits, reason = _value_fits_domain(value, domain)
        if not fits:
            violations.append(DomainViolation(field=field_name, value=value, domain=domain, reason=reason))
    return violations


@dataclass
class ReturnContractReport:
    """The full return-contract verdict for one completed UI run: missing + out-of-domain."""

    missing: list[str] = dc_field(default_factory=list)
    out_of_domain: list[DomainViolation] = dc_field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.missing or self.out_of_domain)

    @property
    def violated_fields(self) -> list[str]:
        return list(self.missing) + [v.field for v in self.out_of_domain]

    def describe(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append("实际读取结果为空：" + "、".join(self.missing))
        for violation in self.out_of_domain:
            parts.append(violation.describe())
        return "；".join(parts)


def check_return_contract(run: object, reads: dict[str, str]) -> ReturnContractReport:
    """The RETURN-CHECK op: does the extraction satisfy the declared output contract?"""
    return ReturnContractReport(
        missing=missing_ui_return_fields(run, reads),
        out_of_domain=out_of_domain_return_fields(run, reads),
    )


def tighten_ui_return_run(
    run: object,
    missing: list[str],
    reads: dict[str, str],
    *,
    attempt: int,
    violations: Sequence[DomainViolation] = (),
) -> object:
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
    bad_fields = list(missing) + [v.field for v in violations]
    bad_text = "、".join(str(field) for field in bad_fields)
    missing_text = "、".join(str(field) for field in missing)
    present = {
        str(field): str(value).strip()
        for field, value in reads.items()
        if str(value).strip()
    }
    present_text = "、".join(f"{field}={value}" for field, value in present.items()) or "无"
    base_success = str(getattr(run, "success_condition", "") or f"完成「{getattr(run, 'name', '当前子目标')}」")
    base_read_spec = str(getattr(run, "read_spec", "") or "")
    violation_text = "".join(
        f"字段「{v.field}」上次读到「{v.value}」但{v.reason}——那不是有效返回值，不要再读同一处；"
        for v in violations
    )
    recovery = (
        f"返回字段恢复尝试 {attempt}: 当前完成帧未读到所有必需字段的有效值。"
        f"已读非空值：{present_text}；缺失字段：{missing_text or '无'}。"
        f"{violation_text}"
        f"只有当这些字段都能从界面明确读取到有效非空值时才算完成：{'、'.join(returns)}。"
        "如果当前屏幕不可见，不要验收完成；继续执行必要的页面内操作，例如等待、滚动、"
        "打开可见的详情/统计/菜单入口、或使用页面搜索，直到缺失字段的具体值可见。"
    )
    name = str(getattr(run, "name", "当前子目标"))
    return run.model_copy(update={
        "name": f"{name}（继续定位返回字段：{bad_text}）",
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
    contract_failure = "实际读取结果为空" in directive or "返回字段合同未满足" in directive
    if not contract_failure or "返回字段" not in directive:
        return program
    if not hasattr(program, "statements") or not hasattr(program, "model_copy"):
        return program

    from gui_agent.core.orchestrator.program import Read, Run

    statements = list(getattr(program, "statements", []) or [])
    if not statements:
        return program
    first = statements[0]
    if not isinstance(first, Read) or not first.returns:
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
    # PROMOTION = re-classification, not a field rewrite: a read that needs interaction becomes a
    # COMMAND. Rebuild as a base Run explicitly — model_copy(update={"kind": ...}) on a Read IR
    # node would skip validation and yield a Read instance lying about its kind.
    statements[0] = Run(
        **{
            **first.model_dump(),
            "kind": "navigation",
            "success_condition": f"{success}\n{recovery}",
            "read_spec": f"{read_spec}\n{recovery}".strip(),
        }
    )
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


# ── kickback = 类型化异常（infeasible 的结构化载荷 + 重规划服从校验）───────────────────
# Feasibility 判死时,verdict 的 dead_route/required_route 以标记折叠进 directive 单通道
# （supervisor/milestone/feasibility.compose_directive）;这里反解并对 redecompose 的输出做
# 确定性服从校验——「禁用的机制不得再现、规定的路线必须出现」,不服从 = 调用方锐化重试。
# 无标记的 directive（空返回/data_query 失败等 loop 内联指令）没有结构化载荷,校验自然 no-op。

DEAD_ROUTE_MARKER = "【死路｜禁止再用】"
REQUIRED_ROUTE_MARKER = "【规定路线】"

_ANCHOR_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{3,}")
_TIGHTEN_SUFFIX_RE = re.compile(r"（继续定位返回字段：[^）]*）")


@dataclass
class KickbackDirective:
    """The typed kickback exception, parsed back from the marked directive prose."""

    dead_route: str = ""
    required_route: str = ""
    text: str = ""

    @property
    def is_typed(self) -> bool:
        return bool(self.dead_route or self.required_route)


def parse_kickback_directive(directive: str) -> KickbackDirective:
    """Parse the 【死路｜禁止再用】/【规定路线】 markers out of a directive string."""
    text = str(directive or "")
    dead = ""
    required = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(DEAD_ROUTE_MARKER):
            dead = stripped[len(DEAD_ROUTE_MARKER):].strip()
        elif stripped.startswith(REQUIRED_ROUTE_MARKER):
            required = stripped[len(REQUIRED_ROUTE_MARKER):].strip()
    return KickbackDirective(dead_route=dead, required_route=required, text=text)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _anchor_tokens(text: str) -> list[str]:
    """Distinctive machine-comparable tokens (alnum, len>=3) — column/control/entity names."""
    return [t.lower() for t in _ANCHOR_TOKEN_RE.findall(str(text or ""))]


def _run_texts(program: object) -> list[tuple[str, str]]:
    """(kind, normalized name+success_condition+sql) for every Run in the program (recursive)."""
    from gui_agent.core.orchestrator.program import ForEach, If, RunLike

    out: list[tuple[str, str]] = []

    def walk(stmts) -> None:
        for s in stmts or []:
            if isinstance(s, RunLike):
                out.append((s.kind, _norm_text(f"{s.name} {s.success_condition} {getattr(s, 'sql', '')}")))
            elif isinstance(s, If):
                walk(s.then)
                walk(s.otherwise)
            elif isinstance(s, ForEach):
                walk(s.body)

    walk(getattr(program, "statements", None) or [])
    for fn in getattr(program, "functions", None) or []:
        walk(getattr(fn, "body", None) or [])
    return out


def kickback_adherence_issues(
    program: object,
    directive: KickbackDirective,
    *,
    failed_run: object = None,
) -> list[str]:
    """Deterministic adherence check of a re-decomposed program against a TYPED kickback.

    Conservative by construction（只报高置信违规,宁漏不误杀）:
    - dead-route reuse: the dead_route's anchor tokens (or its full normalized phrase) all land
      in ONE run's text → the banned mechanism came back, possibly re-worded.
    - failed-run reappearance: a run with the same kind and (normalized) name as the run that was
      judged infeasible → the same dead milestone was re-planned verbatim.
    - required-route ignored: the required route has anchor tokens and NONE of them appear
      anywhere in the program.
    Untyped directives (no markers) yield no issues — inline recovery directives (empty returns /
    data_query failures) legitimately revisit similar steps."""
    issues: list[str] = []
    if not directive.is_typed:
        return issues
    run_texts = _run_texts(program)
    program_text = " ".join(text for _, text in run_texts)

    if directive.dead_route:
        dead_norm = _norm_text(directive.dead_route)
        # Discriminative tokens only: an anchor shared with the required route (e.g. the target
        # attribute "rating" that BOTH the dead filter and the prescribed drill mention) cannot
        # distinguish the two mechanisms and must not count as reuse evidence.
        required_tokens = set(_anchor_tokens(directive.required_route))
        dead_tokens = [t for t in _anchor_tokens(directive.dead_route) if t not in required_tokens]
        for _, text in run_texts:
            phrase_hit = len(dead_norm) >= 6 and dead_norm in text
            token_hit = bool(dead_tokens) and all(t in text for t in dead_tokens)
            if phrase_hit or token_hit:
                issues.append(f"被禁机制再现：「{directive.dead_route}」仍出现在某个步骤中")
                break
        if failed_run is not None and getattr(failed_run, "kind", "") not in {"read", "data_query"}:
            failed_name = _norm_text(_TIGHTEN_SUFFIX_RE.sub("", str(getattr(failed_run, "name", ""))))
            if len(failed_name) >= 8:
                for kind, text in run_texts:
                    if kind == getattr(failed_run, "kind", "") and failed_name in text:
                        issues.append(f"被判不可行的原 milestone「{failed_name[:40]}」原样重现")
                        break

    if directive.required_route:
        req_tokens = _anchor_tokens(directive.required_route)
        if req_tokens and not any(t in program_text for t in req_tokens):
            issues.append(f"规定路线未被采用：「{directive.required_route}」的关键机制词未出现在新计划中")
    return issues


def sharpen_kickback_directive(directive: str, issues: Sequence[str]) -> str:
    """Append an adherence-violation notice to a kickback directive for ONE sharpened retry.

    Names the violations and re-states the ban/route rules using the ABI marker constants (so the
    marker vocabulary never drifts from parse/compose). The caller owns the retry control-flow
    (budget, re-check, pick-better); this only builds the sharpened prose."""
    return (
        directive
        + "\n\n⚠️ 你上一版重规划违反了纠正指令：" + "；".join(issues)
        + f"。必须完全避开{DEAD_ROUTE_MARKER}点名的机制（包括换说法重写它），"
        + f"并把{REQUIRED_ROUTE_MARKER}作为新计划的主干路线。"
    )


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
    if _is_query_run(run):
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
