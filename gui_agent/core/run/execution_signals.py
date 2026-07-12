"""Platform-neutral evidence fusion for interactive statement execution.

The supervisor receives several independent signals: an event may have crossed the GUI
boundary, the page may have responded, and the requested postcondition may or may not be
confirmed.  This module keeps those axes separate and gives deterministic adapter claims
precedence only inside the domains for which they are authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal

from gui_agent.core.schemas import Milestone
CompletionMode = Literal[
    "arrival",
    "filter_state",
    "filter_state_with_result",
    "mutation",
    "read",
    "verification",
]
ClaimDomain = Literal[
    "action.execution",
    "action.write",
    "action.target",
    "page.response",
    "filter.state",
    "control.state",
    "business.outcome",
    "result.availability",
    "inventory.coverage",
    "execution.delegation",
]
ClaimValue = Literal[
    "confirmed",
    "contradicted",
    "unverified",
    "unknown",
    "partial",
    "complete",
]
CompletionStatus = Literal[
    "pending",
    "satisfied",
    "contradicted",
    "delegated",
]


@dataclass(frozen=True)
class ExecutionContract:
    """Internal execution contract derived from a DSL Run/Milestone.

    This is intentionally not part of the DSL wire format.  It describes how the executor
    should interpret evidence for one already-compiled interactive statement.
    """

    statement_id: str
    kind: str
    output_fields: tuple[str, ...] = ()
    read_spec: str = ""
    require_fresh_action: bool = False
    require_terminal_dispatch: bool = False
    completion_mode: CompletionMode = "verification"
    mutation_mode: Literal["ensure", "change"] = "change"
    target_controls: tuple[str, ...] = ()
    target_values: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_milestone(cls, milestone: Milestone) -> "ExecutionContract":
        if milestone.kind == "navigation":
            mode: CompletionMode = "arrival"
        elif milestone.kind == "filter":
            mode = "filter_state_with_result" if milestone.returns else "filter_state"
        elif milestone.kind == "action":
            mode = "mutation"
        elif milestone.kind == "collection":
            mode = "read"
        else:
            mode = "verification"
        return cls(
            statement_id=milestone.id,
            kind=milestone.kind,
            output_fields=tuple(milestone.returns or ()),
            read_spec=milestone.read_spec or "",
            require_fresh_action=bool(milestone.require_fresh_action),
            completion_mode=mode,
            mutation_mode=milestone.mutation_mode,
            target_controls=tuple(milestone.target_controls or ()),
            target_values=tuple((milestone.target_values or {}).items()),
        )


@dataclass(frozen=True)
class EvidenceClaim:
    """One typed assertion from an adapter, runtime ledger, or checker."""

    domain: ClaimDomain
    value: ClaimValue
    source_type: str
    scope: str
    evidence: str = ""
    authoritative_for: tuple[str, ...] = ()
    freshness: str = "turn"
    coverage: str = "unknown"

    @property
    def authoritative(self) -> bool:
        return self.domain in self.authoritative_for


@dataclass(frozen=True)
class CompletionEvaluation:
    status: CompletionStatus
    reason: str
    completion_status: Literal[
        "confirmed", "accepted_unverified", "failed", "in_progress"
    ] = "in_progress"
    used_claims: tuple[EvidenceClaim, ...] = ()
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConstraintEntry:
    text: str
    scope: str
    source: str = "runtime"


@dataclass
class ConstraintLedger:
    """Typed runtime constraints whose lifetime is explicit.

    Static task constraints use ``scope='task'``.  Runtime loop/no-effect facts should use the
    current milestone or row scope and therefore cannot poison a later execution context.
    """

    entries: list[ConstraintEntry] = field(default_factory=list)

    def add(self, text: str, *, scope: str, source: str = "runtime") -> None:
        entry = ConstraintEntry(text=text, scope=scope, source=source)
        if entry not in self.entries:
            self.entries.append(entry)

    def visible(self, scope: str) -> list[str]:
        return [entry.text for entry in self.entries if entry.scope in {"task", scope}]

    def clear_scope(self, scope: str) -> None:
        self.entries = [entry for entry in self.entries if entry.scope != scope]


@dataclass(frozen=True)
class ProvisionalOutcome:
    statement_id: str
    scope: str
    status: Literal["accepted_unverified"] = "accepted_unverified"
    evidence: tuple[str, ...] = ()


@dataclass
class ProvisionalOutcomeLedger:
    """Reportable provisional outcomes; no rollback behavior is attached to this ledger."""

    entries: list[ProvisionalOutcome] = field(default_factory=list)

    def record(self, outcome: ProvisionalOutcome) -> None:
        if outcome not in self.entries:
            self.entries.append(outcome)


def claim(
    domain: ClaimDomain,
    value: ClaimValue,
    *,
    source_type: str,
    scope: str,
    evidence: str = "",
    authoritative: bool = False,
    freshness: str = "turn",
    coverage: str = "unknown",
) -> EvidenceClaim:
    """Concise claim constructor used by existing deterministic Gate producers."""
    return EvidenceClaim(
        domain=domain,
        value=value,
        source_type=source_type,
        scope=scope,
        evidence=evidence,
        authoritative_for=(domain,) if authoritative else (),
        freshness=freshness,
        coverage=coverage,
    )


class CompletionEvaluator:
    """Evaluate whether typed evidence satisfies an execution contract.

    This class has no authority over action proposals or dispatch.  Its result is consumed by the
    milestone policy, which remains the sole owner of advance/recover/fail transitions.
    """

    @staticmethod
    def _claims(
        claims: Iterable[EvidenceClaim], domain: ClaimDomain, scope: str
    ) -> list[EvidenceClaim]:
        return [item for item in claims if item.domain == domain and item.scope == scope]

    @staticmethod
    def _best(items: Iterable[EvidenceClaim]) -> EvidenceClaim | None:
        values = list(items)
        if not values:
            return None
        # Deterministic authority wins inside its declared domain.  A contradiction then wins
        # over confirmation so a known validation failure cannot be hidden by a stale success cue.
        return max(
            values,
            key=lambda item: (
                item.authoritative,
                item.value == "contradicted",
                item.value == "confirmed",
                item.freshness == "turn",
            ),
        )

    def decide(
        self,
        contract: ExecutionContract,
        claims: Iterable[EvidenceClaim],
        *,
        scope: str,
    ) -> CompletionEvaluation:
        scoped = tuple(item for item in claims if item.scope == scope)
        outcome = self._best(self._claims(scoped, "business.outcome", scope))
        if outcome is not None and outcome.value == "contradicted":
            return CompletionEvaluation(
                status="contradicted",
                reason=outcome.evidence or "业务后置状态已被明确证伪",
                used_claims=(outcome,),
            )

        execution = self._best(self._claims(scoped, "action.execution", scope))
        write = self._best(self._claims(scoped, "action.write", scope))
        response = self._best(self._claims(scoped, "page.response", scope))
        target = self._best(self._claims(scoped, "action.target", scope))
        filter_state = self._best(self._claims(scoped, "filter.state", scope))
        control_state = self._best(self._claims(scoped, "control.state", scope))
        result = self._best(self._claims(scoped, "result.availability", scope))
        delegation = self._best(
            self._claims(scoped, "execution.delegation", scope)
        )

        if delegation is not None and delegation.value == "confirmed":
            return CompletionEvaluation(
                status="delegated",
                reason=delegation.evidence or "执行责任已委托给后续执行单元",
                used_claims=(delegation,),
            )

        if (
            target is not None
            and target.value == "contradicted"
            and not (outcome is not None and outcome.value == "confirmed")
        ):
            return CompletionEvaluation(
                status="contradicted",
                reason=target.evidence or "动作命中了错误目标",
                used_claims=(target,),
            )

        if contract.completion_mode == "filter_state_with_result":
            if filter_state is not None and filter_state.value == "confirmed":
                # A zero-row result is a valid return value.  The following interpreter branch,
                # not this milestone, decides whether to run a fallback search.
                used = (filter_state,) + ((result,) if result is not None else ())
                return CompletionEvaluation(
                    status="satisfied",
                    completion_status="confirmed",
                    reason=filter_state.evidence or "目标筛选状态已权威确认",
                    used_claims=used,
                )
            return CompletionEvaluation("pending", "筛选状态尚未权威确认")

        if contract.completion_mode == "filter_state":
            if filter_state is not None and filter_state.value == "confirmed":
                if outcome is not None and outcome.value == "confirmed":
                    return CompletionEvaluation(
                        "satisfied",
                        outcome.evidence or filter_state.evidence,
                        "confirmed",
                        (filter_state, outcome),
                    )
                return CompletionEvaluation("pending", "筛选已应用，仍需结果/验收信号")
            return CompletionEvaluation("pending", "筛选状态尚未权威确认")

        if contract.completion_mode == "arrival":
            if outcome is not None and outcome.value == "confirmed":
                return CompletionEvaluation(
                    "satisfied", outcome.evidence or "目标页面状态已确认", "confirmed", (outcome,)
                )
            # A generic response proves only that something happened, not that the destination is
            # correct.  It intentionally cannot complete navigation by itself.
            return CompletionEvaluation("pending", "尚无目标页面身份的确认信号")

        if contract.completion_mode == "mutation":
            if control_state is not None and control_state.value == "contradicted":
                return CompletionEvaluation(
                    "pending",
                    control_state.evidence or "声明的目标字段尚未全部达到目标值",
                    conflicts=("target.values.incomplete",),
                    used_claims=(control_state,),
                )
            write_confirmed = bool(write is not None and write.value == "confirmed")
            commit_confirmed = bool(
                execution is not None
                and execution.value == "confirmed"
                and execution.source_type == "runtime.commit_dispatch"
            )
            # ``ensure`` may accept a genuinely pre-existing terminal state without touching it.
            # Once this execution scope has written a target field, however, the observed control
            # values are only draft state until the statement's declared persistence boundary is
            # dispatched. Do not let the idempotent/pre-existing fast path swallow that commit.
            if (
                contract.mutation_mode == "ensure"
                and contract.require_terminal_dispatch
                and write_confirmed
                and not commit_confirmed
            ):
                return CompletionEvaluation(
                    "pending",
                    "目标字段已在本轮写入，但声明的终端提交尚未派发；"
                    "当前控件值只能证明草稿状态，不能证明已持久化",
                    conflicts=("action.commit.required",),
                    used_claims=tuple(
                        item for item in (write, control_state, outcome) if item is not None
                    ),
                )
            if outcome is not None and outcome.value == "confirmed":
                if contract.mutation_mode == "ensure":
                    return CompletionEvaluation(
                        "satisfied",
                        outcome.evidence or "幂等目标状态已确认",
                        "confirmed",
                        (outcome,),
                    )
                if contract.require_terminal_dispatch and not commit_confirmed:
                    return CompletionEvaluation(
                        "pending",
                        "目标状态看似满足，但该执行单元要求的终端提交尚未派发；"
                        "因此缺少本轮产生写操作的执行证据",
                        conflicts=("action.commit.required",),
                    )
                if not write_confirmed:
                    return CompletionEvaluation(
                        "pending",
                        "目标状态看似满足，但 change mutation 缺少本轮产生写操作的目标写入证据",
                        conflicts=("action.write.required",),
                    )
                if contract.require_fresh_action and not (
                    execution is not None and execution.value == "confirmed"
                ):
                    return CompletionEvaluation(
                        "pending",
                        "当前目标状态看似已存在，但缺少本轮产生写操作的执行证据",
                        conflicts=("action.execution.required",),
                    )
                return CompletionEvaluation(
                    "satisfied",
                    outcome.evidence or "业务后置状态已确认",
                    "confirmed",
                    tuple(item for item in (write, execution, outcome) if item is not None),
                )
            if (
                execution is not None
                and execution.value == "confirmed"
                and execution.source_type == "runtime.commit_dispatch"
                and not write_confirmed
            ):
                return CompletionEvaluation(
                    "pending",
                    "终端提交已派发，但当前执行作用域缺少目标写入；提交不能代替业务字段写入",
                    conflicts=("action.write.required",),
                )
            if (
                execution is not None
                and execution.value == "confirmed"
                and execution.source_type == "runtime.commit_dispatch"
                and write_confirmed
            ):
                if response is None or response.value != "contradicted":
                    return CompletionEvaluation(
                        "satisfied",
                        "终端副作用已派发且没有矛盾证据；结果反馈通道不可用或尚未出现",
                        "accepted_unverified",
                        tuple(item for item in (execution, response) if item is not None),
                    )
            checkbox_state_is_safe_terminal = bool(
                control_state is not None
                and control_state.source_type == "obs.dom_ax.checked"
            )
            if (
                control_state is not None
                and control_state.value == "confirmed"
                and (not contract.require_fresh_action or checkbox_state_is_safe_terminal)
            ):
                return CompletionEvaluation(
                    "satisfied", control_state.evidence or "目标控件状态已确认", "confirmed", (control_state,)
                )
            return CompletionEvaluation("pending", "动作结果尚未确认")

        if outcome is not None and outcome.value == "confirmed":
            return CompletionEvaluation(
                "satisfied", outcome.evidence or "验收状态已确认", "confirmed", (outcome,)
            )
        if control_state is not None and control_state.value == "confirmed":
            return CompletionEvaluation(
                "satisfied", control_state.evidence or "控件状态已确认", "confirmed", (control_state,)
            )
        return CompletionEvaluation("pending", "当前证据不足以完成执行单元")

def _normalize_target(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _target_tokens(value: str) -> frozenset[str]:
    return frozenset(
        re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", (value or "").lower())
    )


def target_matches_declared(
    target: str,
    declared: Iterable[str],
    *,
    allow_less_specific: bool = True,
) -> bool:
    proposed = _normalize_target(target)
    expected_targets = {
        str(value): _normalize_target(value)
        for value in declared
        if _normalize_target(value)
    }
    proposed_tokens = _target_tokens(target)
    return bool(
        proposed
        and any(
            proposed == expected
            or expected in proposed
            or (
                _target_tokens(raw_expected)
                and _target_tokens(raw_expected).issubset(proposed_tokens)
            )
            or (allow_less_specific and proposed in expected)
            or (
                allow_less_specific
                and proposed_tokens
                and proposed_tokens.issubset(_target_tokens(raw_expected))
            )
            for raw_expected, expected in expected_targets.items()
        )
    )
