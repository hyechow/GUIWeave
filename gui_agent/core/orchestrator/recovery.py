"""统一恢复账本 —— 异常体系 Stage A（分类 + 记账，行为零变化）。

架构自检问题①（docs/milestone_as_function.md「架构自检」）：九种恢复/重试机制各有各的
预算常数与触发条件，互不知情，预算乘积无人度量。Stage A 先立【异常分类】（作为类型，
不是控制流）+【单一账本】（任何机制触发恢复都记一条事件），预算常数与控制流原样不动；
Stage B（live 后）再用账本轨迹设计全局预算与升级链（载体 = loop.py 状态机化）。

异常四分类（跨 FFI 边界的事件才入账；执行器内部的 action replan / checker 重试留在
ABI 之下 —— 被调用方自己的重试对调用方不可见，这是 FFI 纪律的自然延伸）：

- ``compile_error``       编译期：validator/preflight 反馈重试。Stage A 里 decompose 的
                          重试事件走既有 attempt_observer 观测路（scripts/validator_retry_
                          efficacy.py），暂不进运行时账本（入口闭包不透传 observer）。
- ``contract_violation``  FFI 出参合同违约：空返回/域违约 → tighten 有界恢复、升格手术、
                          或升级为 kickback。
- ``infeasible_route``    不可行路线：feasibility kickback → redecompose（+ adherence 锐化）。
- ``data_source_error``   数据源/查询错误：data_query 失败、SQL 运行时修复、非 UI kickback。

账本继承 ``ReturnRecoveryLedger``（原 callframe 的空返回预算，按调用点隔离），所以
loop 里同一个对象既管既有预算又记事件 —— 泛化而非新造。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

RecoveryClass = Literal[
    "compile_error",
    "contract_violation",
    "infeasible_route",
    "data_source_error",
]

# 返回字段为空时，最多把当前 UI run 收紧后重新驱动几次（原 callframe 常量，随账本搬家）
MAX_EMPTY_RETURN_RECOVERIES = 3


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
