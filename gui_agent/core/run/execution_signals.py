"""Platform-neutral evidence fusion for interactive statement execution.

The supervisor receives several independent signals: an event may have crossed the GUI
boundary, the page may have responded, and the requested postcondition may or may not be
confirmed.  This module keeps those axes separate and gives deterministic adapter claims
precedence only inside the domains for which they are authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from gui_agent.core.schemas import ActionFamily, Milestone
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
FusionAction = Literal[
    "continue",
    "complete",
    "replan",
    "allow_action",
    "reject_action",
    "delegate",
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
class FusionDecision:
    action: FusionAction
    reason: str
    completion_status: Literal[
        "confirmed", "accepted_unverified", "failed", "in_progress"
    ] = "in_progress"
    used_claims: tuple[EvidenceClaim, ...] = ()
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionConstraint:
    """One scoped restriction on the next planner proposal.

    Completion evidence answers whether a statement is done. Action constraints answer which
    atomic family may safely execute next. Keeping them separate prevents an unresolved form
    field from being misreported as a failed business outcome.
    """

    scope: str
    source_type: str
    evidence: str
    allowed_families: tuple[ActionFamily, ...] = ()
    blocked_families: tuple[ActionFamily, ...] = ()


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


class SignalFusionArbiter:
    """Decide completion from typed claims using a small precedence table."""

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
    ) -> FusionDecision:
        scoped = tuple(item for item in claims if item.scope == scope)
        outcome = self._best(self._claims(scoped, "business.outcome", scope))
        if outcome is not None and outcome.value == "contradicted":
            return FusionDecision(
                action="replan",
                reason=outcome.evidence or "业务后置状态已被明确证伪",
                used_claims=(outcome,),
            )

        execution = self._best(self._claims(scoped, "action.execution", scope))
        response = self._best(self._claims(scoped, "page.response", scope))
        target = self._best(self._claims(scoped, "action.target", scope))
        filter_state = self._best(self._claims(scoped, "filter.state", scope))
        control_state = self._best(self._claims(scoped, "control.state", scope))
        result = self._best(self._claims(scoped, "result.availability", scope))
        delegation = self._best(
            self._claims(scoped, "execution.delegation", scope)
        )

        if delegation is not None and delegation.value == "confirmed":
            return FusionDecision(
                action="delegate",
                reason=delegation.evidence or "执行责任已委托给后续执行单元",
                used_claims=(delegation,),
            )

        if (
            target is not None
            and target.value == "contradicted"
            and not (outcome is not None and outcome.value == "confirmed")
        ):
            return FusionDecision(
                action="replan",
                reason=target.evidence or "动作命中了错误目标",
                used_claims=(target,),
            )

        if contract.completion_mode == "filter_state_with_result":
            if filter_state is not None and filter_state.value == "confirmed":
                # A zero-row result is a valid return value.  The following interpreter branch,
                # not this milestone, decides whether to run a fallback search.
                used = (filter_state,) + ((result,) if result is not None else ())
                return FusionDecision(
                    action="complete",
                    completion_status="confirmed",
                    reason=filter_state.evidence or "目标筛选状态已权威确认",
                    used_claims=used,
                )
            return FusionDecision("continue", "筛选状态尚未权威确认")

        if contract.completion_mode == "filter_state":
            if filter_state is not None and filter_state.value == "confirmed":
                if outcome is not None and outcome.value == "confirmed":
                    return FusionDecision(
                        "complete",
                        outcome.evidence or filter_state.evidence,
                        "confirmed",
                        (filter_state, outcome),
                    )
                return FusionDecision("continue", "筛选已应用，仍需结果/验收信号")
            return FusionDecision("continue", "筛选状态尚未权威确认")

        if contract.completion_mode == "arrival":
            if outcome is not None and outcome.value == "confirmed":
                return FusionDecision(
                    "complete", outcome.evidence or "目标页面状态已确认", "confirmed", (outcome,)
                )
            # A generic response proves only that something happened, not that the destination is
            # correct.  It intentionally cannot complete navigation by itself.
            return FusionDecision("continue", "尚无目标页面身份的确认信号")

        if contract.completion_mode == "mutation":
            if outcome is not None and outcome.value == "confirmed":
                if contract.require_terminal_dispatch and not (
                    execution is not None
                    and execution.value == "confirmed"
                    and execution.source_type == "runtime.commit_dispatch"
                ):
                    return FusionDecision(
                        "continue",
                        "目标状态看似满足，但该执行单元要求的终端提交尚未派发；"
                        "因此缺少本轮产生写操作的执行证据",
                    )
                if contract.require_fresh_action and not (
                    execution is not None and execution.value == "confirmed"
                ):
                    return FusionDecision(
                        "continue",
                        "当前目标状态看似已存在，但缺少本轮产生写操作的执行证据",
                    )
                return FusionDecision(
                    "complete", outcome.evidence or "业务后置状态已确认", "confirmed", (outcome,)
                )
            if (
                execution is not None
                and execution.value == "confirmed"
                and execution.source_type == "runtime.commit_dispatch"
            ):
                if response is None or response.value != "contradicted":
                    return FusionDecision(
                        "complete",
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
                return FusionDecision(
                    "complete", control_state.evidence or "目标控件状态已确认", "confirmed", (control_state,)
                )
            return FusionDecision("continue", "动作结果尚未确认")

        if outcome is not None and outcome.value == "confirmed":
            return FusionDecision(
                "complete", outcome.evidence or "验收状态已确认", "confirmed", (outcome,)
            )
        if control_state is not None and control_state.value == "confirmed":
            return FusionDecision(
                "complete", control_state.evidence or "控件状态已确认", "confirmed", (control_state,)
            )
        return FusionDecision("continue", "当前证据不足以完成执行单元")

    def validate_proposal(
        self,
        family: ActionFamily,
        constraints: Iterable[ActionConstraint],
        *,
        scope: str,
    ) -> FusionDecision:
        """Validate one planner action family against fresh scoped constraints."""
        scoped = tuple(item for item in constraints if item.scope == scope)
        blocked = tuple(item for item in scoped if family in item.blocked_families)
        if blocked:
            return FusionDecision(
                action="reject_action",
                reason="；".join(item.evidence for item in blocked if item.evidence),
                conflicts=tuple(item.evidence for item in blocked if item.evidence),
            )

        allowed_sets = [set(item.allowed_families) for item in scoped if item.allowed_families]
        if allowed_sets:
            allowed = set.intersection(*allowed_sets)
            if family not in allowed:
                expected = ", ".join(sorted(allowed)) or "<none>"
                reasons = [item.evidence for item in scoped if item.allowed_families and item.evidence]
                return FusionDecision(
                    action="reject_action",
                    reason=(
                        f"当前结构化控件状态只允许动作族 [{expected}]，"
                        f"proposal={family}。" + "；".join(reasons)
                    ),
                    conflicts=tuple(reasons),
                )

        return FusionDecision(
            action="allow_action",
            reason="planner proposal satisfies current action constraints",
        )


_ACTION_FAMILY_TYPES: dict[ActionFamily, frozenset[str]] = {
    "input": frozenset({"type", "clear_text"}),
    "select": frozenset({"select_option", "tap", "click"}),
    "activate": frozenset({"tap", "click", "press_enter"}),
    "navigate": frozenset({"navigate", "back", "new_tab", "select_tab", "tap", "click"}),
    "iterate": frozenset({"scroll", "drag"}),
    "commit": frozenset({"tap", "click", "press_enter"}),
    "unknown": frozenset(),
}


def validate_action_family(family: ActionFamily, action_type: str) -> tuple[bool, str]:
    """Validate a concrete primitive before it crosses the GUI boundary."""
    if family == "unknown":
        return True, ""
    actual = (action_type or "").lower()
    allowed = _ACTION_FAMILY_TYPES[family]
    if actual in allowed:
        return True, ""
    return False, f"动作族 {family} 不允许执行 primitive={actual or '<empty>'}"
