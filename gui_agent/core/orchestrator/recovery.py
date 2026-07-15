"""统一恢复账本 —— 异常体系 Stage A（分类 + 记账，行为零变化）。

架构背景见 docs/dsl_runtime_architecture.md：恢复按 statement 边界分类，Milestone 内部重试
留在执行器；跨 statement 的恢复由 Program runtime 记录和升级。历史上各机制的
预算常数与触发条件，互不知情，预算乘积无人度量。Stage A 先立【异常分类】（作为类型，
不是控制流）+【单一账本】（任何机制触发恢复都记一条事件），预算常数与控制流原样不动；
Stage B（live 后）再用账本轨迹设计全局预算与升级链（载体 = loop.py 状态机化）。

异常四分类（跨 statement 执行边界的事件才入账；Milestone 内部的 action replan /
checker 重试仍由交互执行器自己处理）：

- ``compile_error``       编译期：validator/preflight 反馈重试。Stage A 里 decompose 的
                          重试事件走既有 attempt_observer 观测路（scripts/validator_retry_
                          efficacy.py），暂不进运行时账本（入口闭包不透传 observer）。
- ``contract_violation``  FFI 出参合同违约：空返回/域违约 → tighten 有界恢复、升格手术、
                          或升级为 kickback。
- ``infeasible_route``    不可行路线：feasibility kickback → redecompose（+ adherence 锐化）。
- ``data_source_error``   数据源/查询错误：data_query 失败、SQL 运行时修复、非 UI kickback。

账本继承 ``ReturnRecoveryLedger``（空返回预算，按 statement 位置隔离），所以 loop 里
同一个对象既管既有预算又记事件。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, Optional, Sequence

from .contracts import DomainViolation

RecoveryClass = Literal[
    "compile_error",
    "contract_violation",
    "infeasible_route",
    "data_source_error",
]

# 返回字段为空时，最多把当前交互 statement 收紧后重新驱动几次。
MAX_EMPTY_RETURN_RECOVERIES = 3
MAX_KICKBACK_REPLANS = 1


@dataclass(frozen=True)
class RecoveryEvent:
    """One recovery-mechanism firing. Append-only; the outcome is what happened AT record time
    (e.g. "attempt 2/3" / "replanned" / "exhausted_honest_fail"), not a mutable status."""

    cls: RecoveryClass
    mechanism: str  # tighten_return / kickback_redecompose / adherence_sharpen / interactive_promotion / sql_repair / data_query_failure …
    site: str       # where it fired: run var/name, or a stage name
    detail: str = ""
    outcome: str = ""

    def to_dict(self) -> dict:
        return {
            "class": self.cls,
            "mechanism": self.mechanism,
            "site": self.site,
            "detail": self.detail,
            "outcome": self.outcome,
        }


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


class RecoveryLedger(ReturnRecoveryLedger):
    """The task-wide recovery ledger: the inherited per-call-site empty-return budget PLUS an
    append-only event log every recovery mechanism reports to. Stage A: record only — budgets
    and control flow stay with their mechanisms; Stage B derives global budgets from the trace."""

    def __init__(self, max_attempts: int = MAX_EMPTY_RETURN_RECOVERIES):
        super().__init__(max_attempts=max_attempts)
        self.events: list[RecoveryEvent] = []

    def record(
        self,
        cls: RecoveryClass,
        mechanism: str,
        site: str,
        *,
        detail: str = "",
        outcome: str = "",
    ) -> RecoveryEvent:
        event = RecoveryEvent(cls=cls, mechanism=mechanism, site=str(site or ""),
                              detail=detail, outcome=outcome)
        self.events.append(event)
        return event

    def summary(self) -> dict:
        """Serializable trace for the run result / report card（live 归因的第一手材料）。"""
        by_class: dict[str, int] = {}
        by_mechanism: dict[str, int] = {}
        for e in self.events:
            by_class[e.cls] = by_class.get(e.cls, 0) + 1
            by_mechanism[e.mechanism] = by_mechanism.get(e.mechanism, 0) + 1
        return {
            "total": len(self.events),
            "by_class": by_class,
            "by_mechanism": by_mechanism,
            "events": [e.to_dict() for e in self.events],
        }


def tighten_ui_return_run(
    run: object,
    missing: list[str],
    reads: dict[str, str],
    *,
    attempt: int,
    violations: Sequence[DomainViolation] = (),
) -> object:
    """Strengthen an interactive statement after its declared outputs were not recovered."""
    if run is None or not hasattr(run, "model_copy"):
        return run
    returns = [str(field) for field in getattr(run, "returns", [])]
    bad_fields = list(missing) + [violation.field for violation in violations]
    bad_text = "、".join(str(field) for field in bad_fields)
    missing_text = "、".join(str(field) for field in missing)
    present = {
        str(field): str(value).strip()
        for field, value in reads.items()
        if str(value).strip()
    }
    present_text = "、".join(f"{field}={value}" for field, value in present.items()) or "无"
    base_success = str(
        getattr(run, "success_condition", "")
        or f"完成「{getattr(run, 'name', '当前子目标')}」"
    )
    base_read_spec = str(getattr(run, "read_spec", "") or "")
    violation_text = "".join(
        f"字段「{violation.field}」上次读到「{violation.value}」但{violation.reason}"
        "——那不是有效返回值，不要再读同一处；"
        for violation in violations
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
    """Promote an insufficient current-frame read into an interactive locating statement."""
    contract_failure = "实际读取结果为空" in directive or "返回字段合同未满足" in directive
    if not contract_failure or "返回字段" not in directive:
        return program
    if not hasattr(program, "statements") or not hasattr(program, "model_copy"):
        return program

    from .program import Read, Run

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
    statements[0] = Run(
        **{
            **first.model_dump(),
            "kind": "navigation",
            "success_condition": f"{success}\n{recovery}",
            "read_spec": f"{read_spec}\n{recovery}".strip(),
        }
    )
    return program.model_copy(update={"statements": statements})


def should_kickback_replan(outcome, redecompose, replan_count: int) -> bool:
    """Whether an infeasible statement outcome should recompile the remaining Program."""
    return bool(
        getattr(outcome, "phase", None) == "infeasible"
        and getattr(outcome, "kickback", None)
        and callable(redecompose)
        and replan_count < MAX_KICKBACK_REPLANS
    )


DEAD_ROUTE_MARKER = "【死路｜禁止再用】"
REQUIRED_ROUTE_MARKER = "【规定路线】"

_ANCHOR_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{3,}")
_TIGHTEN_SUFFIX_RE = re.compile(r"（继续定位返回字段：[^）]*）")


@dataclass
class KickbackDirective:
    """Typed evidence carried by an infeasible Milestone outcome."""

    dead_route: str = ""
    required_route: str = ""
    text: str = ""

    @property
    def is_typed(self) -> bool:
        return bool(self.dead_route or self.required_route)


def parse_kickback_directive(directive: str) -> KickbackDirective:
    """Parse structured route markers from a recovery directive."""
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
    return [token.lower() for token in _ANCHOR_TOKEN_RE.findall(str(text or ""))]


def _run_texts(program: object) -> list[tuple[str, str]]:
    from .program import ForEach, If, RunLike

    out: list[tuple[str, str]] = []

    def walk(statements) -> None:
        for statement in statements or []:
            if isinstance(statement, RunLike):
                out.append((
                    statement.kind,
                    _norm_text(
                        f"{statement.name} {statement.success_condition} "
                        f"{getattr(statement, 'sql', '')}"
                    ),
                ))
            elif isinstance(statement, If):
                walk(statement.then)
                walk(statement.otherwise)
            elif isinstance(statement, ForEach):
                walk(statement.body)

    walk(getattr(program, "statements", None) or [])
    for function in getattr(program, "functions", None) or []:
        walk(getattr(function, "body", None) or [])
    return out


def kickback_adherence_issues(
    program: object,
    directive: KickbackDirective,
    *,
    failed_run: object = None,
) -> list[str]:
    """Conservatively check a recompiled Program against typed route evidence."""
    issues: list[str] = []
    if not directive.is_typed:
        return issues
    run_texts = _run_texts(program)
    program_text = " ".join(text for _, text in run_texts)

    if directive.dead_route:
        dead_norm = _norm_text(directive.dead_route)
        required_tokens = set(_anchor_tokens(directive.required_route))
        dead_tokens = [
            token for token in _anchor_tokens(directive.dead_route)
            if token not in required_tokens
        ]
        for _, text in run_texts:
            phrase_hit = len(dead_norm) >= 6 and dead_norm in text
            token_hit = bool(dead_tokens) and all(token in text for token in dead_tokens)
            if phrase_hit or token_hit:
                issues.append(f"被禁机制再现：「{directive.dead_route}」仍出现在某个步骤中")
                break
        if failed_run is not None and getattr(failed_run, "kind", "") not in {"read", "data_query"}:
            failed_name = _norm_text(
                _TIGHTEN_SUFFIX_RE.sub("", str(getattr(failed_run, "name", "")))
            )
            if len(failed_name) >= 8:
                for kind, text in run_texts:
                    if kind == getattr(failed_run, "kind", "") and failed_name in text:
                        issues.append(f"被判不可行的原 milestone「{failed_name[:40]}」原样重现")
                        break

    if directive.required_route:
        required_tokens = _anchor_tokens(directive.required_route)
        if required_tokens and not any(token in program_text for token in required_tokens):
            issues.append(
                f"规定路线未被采用：「{directive.required_route}」的关键机制词未出现在新计划中"
            )
    return issues


def sharpen_kickback_directive(directive: str, issues: Sequence[str]) -> str:
    """Add deterministic adherence failures to one recompile retry directive."""
    return (
        directive
        + "\n\n⚠️ 你上一版重规划违反了纠正指令：" + "；".join(issues)
        + f"。必须完全避开{DEAD_ROUTE_MARKER}点名的机制（包括换说法重写它），"
        + f"并把{REQUIRED_ROUTE_MARKER}作为新计划的主干路线。"
    )
