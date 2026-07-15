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

from gui_agent.core.schemas import EffectMode, StatementContract, PersistenceMode
from gui_agent.core.run.persistence import PersistenceAssessment
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
    "effect.state",
    "collection.coverage",
]
ClaimValue = Literal[
    "confirmed",
    "contradicted",
    "unmet",
    "unverified",
    "unknown",
    "partial",
    "complete",
]
CompletionStatus = Literal[
    "pending",
    "satisfied",
    "contradicted",
]


@dataclass(frozen=True)
class ExecutionContract:
    """Internal execution contract derived from a DSL Run/StatementContract.

    This is intentionally not part of the DSL wire format.  It describes how the executor
    should interpret evidence for one already-compiled interactive statement.
    """

    statement_id: str
    kind: str
    output_fields: tuple[str, ...] = ()
    read_spec: str = ""
    completion_mode: CompletionMode = "verification"
    effect_mode: EffectMode | None = None
    persistence: PersistenceMode = "immediate"

    @classmethod
    def from_statement(cls, statement: StatementContract) -> "ExecutionContract":
        if statement.kind == "navigation":
            mode: CompletionMode = "arrival"
        elif statement.kind == "filter":
            mode = "filter_state_with_result" if statement.returns else "filter_state"
        elif statement.kind == "action":
            mode = (
                "mutation"
                if action_requires_mutation_evidence(
                    effect_mode=statement.effect_mode,
                    target_values=statement.target_values,
                    persistence=statement.persistence,
                    output_fields=statement.returns,
                )
                else "verification"
            )
        elif statement.kind == "collection":
            mode = "read"
        else:
            mode = "verification"
        return cls(
            statement_id=statement.id,
            kind=statement.kind,
            output_fields=tuple(statement.returns or ()),
            read_spec=statement.read_spec or "",
            completion_mode=mode,
            effect_mode=statement.effect_mode,
            persistence=statement.persistence,
        )


def action_requires_mutation_evidence(
    *,
    effect_mode: EffectMode | None,
    target_values: Iterable[object],
    persistence: PersistenceMode,
    output_fields: Iterable[object],
) -> bool:
    """Whether an action declares a business mutation that needs lifecycle evidence.

    ``action`` is an interactive boundary, not inherently a persistent write.  Opening a record,
    expanding a region, or switching a view may be emitted as an action by a decomposer and is
    complete when its declared state is verified.  Structured target values, a persistence
    boundary, returned action data, or explicit ensure semantics identify actual mutations.
    """
    return bool(
        effect_mode is not None
        or tuple(target_values)
        or persistence == "explicit_commit"
        or tuple(output_fields)
    )


@dataclass(frozen=True)
class EvidenceClaim:
    """One typed assertion from an adapter, runtime ledger, or checker."""

    domain: ClaimDomain
    value: ClaimValue
    source_type: str
    scope: str
    subject_scope: str = ""
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
    next: Literal["complete", "act", "commit", "observe", "recover"] = "act"


@dataclass(frozen=True)
class ConstraintEntry:
    text: str
    scope: str
    source: str = "runtime"


@dataclass
class ConstraintLedger:
    """Typed runtime constraints whose lifetime is explicit.

    Static task constraints use ``scope='task'``.  Runtime loop/no-effect facts should use the
    current statement or row scope and therefore cannot poison a later execution context.
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

    def remove_sources(self, scope: str, sources: Iterable[str]) -> int:
        """Remove scoped transient constraints from selected producers."""
        selected = set(sources)
        before = len(self.entries)
        self.entries = [
            entry
            for entry in self.entries
            if not (entry.scope == scope and entry.source in selected)
        ]
        return before - len(self.entries)


def claim(
    domain: ClaimDomain,
    value: ClaimValue,
    *,
    source_type: str,
    scope: str,
    subject_scope: str = "",
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
        subject_scope=subject_scope,
        evidence=evidence,
        authoritative_for=(domain,) if authoritative else (),
        freshness=freshness,
        coverage=coverage,
    )


class ExecutionCoordinator:
    """Reduce action, effect, and persistence evidence into one control decision.

    The three assessments remain independent.  In particular, dispatch does not prove the
    business effect and a visible draft value does not prove persistence.  This coordinator is
    the only component that combines those facts into ``complete/act/commit/observe/recover``.
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
                item.value == "unmet",
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
        persistence_assessment: PersistenceAssessment | None = None,
    ) -> CompletionEvaluation:
        scoped = tuple(item for item in claims if item.scope == scope)
        execution = self._best(self._claims(scoped, "action.execution", scope))
        write = self._best(self._claims(scoped, "action.write", scope))
        expected_subject = ""
        if contract.completion_mode == "mutation":
            # Only a write identifies the mutated business subject. A commit's execution
            # scope identifies the persistence boundary/page, not the child row or control
            # whose state was observed before the redirect.
            expected_subject = write.subject_scope if write is not None else ""

        def covers_expected_subject(item: EvidenceClaim) -> bool:
            return bool(
                not expected_subject
                or not item.subject_scope
                or item.subject_scope == expected_subject
            )

        effect_claim = self._best(
            item
            for item in self._claims(scoped, "effect.state", scope)
            if (
                item.value == "contradicted"
                and item.source_type == "checker.rejected"
            )
            or covers_expected_subject(item)
        )
        target = self._best(self._claims(scoped, "action.target", scope))
        filter_state = self._best(self._claims(scoped, "filter.state", scope))
        control_state = self._best(
            item
            for item in self._claims(scoped, "control.state", scope)
            if covers_expected_subject(item)
        )
        collection_coverage = self._best(
            self._claims(scoped, "collection.coverage", scope)
        )
        action_delivery = (
            "delivered"
            if execution is not None and execution.value == "confirmed"
            else "failed"
            if execution is not None and execution.value == "contradicted"
            else "not_attempted"
        )
        action_targeting = (
            "confirmed"
            if target is not None and target.value == "confirmed"
            else "contradicted"
            if target is not None and target.value == "contradicted"
            else "unknown"
        )

        # An authoritative reading of the declared controls can disprove a probabilistic checker
        # diagnosis.  This precedence is local to effect assessment; it cannot prove dispatch or
        # persistence.
        outcome_contradicted = bool(
            effect_claim is not None
            and effect_claim.value == "contradicted"
            and action_delivery == "delivered"
        )
        pending_checker_unmet = bool(
            outcome_contradicted
            and effect_claim is not None
            and not effect_claim.authoritative
            and effect_claim.source_type != "checker.rejected"
            and persistence_assessment is not None
            and persistence_assessment.status == "pending"
        )
        if pending_checker_unmet:
            outcome_contradicted = False
        if (
            outcome_contradicted
            and effect_claim is not None
            and not effect_claim.authoritative
            and control_state is not None
            and control_state.value == "confirmed"
            and control_state.authoritative
        ):
            outcome_contradicted = False
        if outcome_contradicted:
            effect_status: Literal["satisfied", "unmet", "contradicted", "unknown"] = (
                "contradicted"
            )
            effect_evidence = effect_claim.evidence if effect_claim is not None else ""
        elif pending_checker_unmet or (
            effect_claim is not None and effect_claim.value == "unmet"
        ) or (
            control_state is not None
            and control_state.value in {"contradicted", "unmet"}
        ):
            effect_status = "unmet"
            effect_evidence = (
                effect_claim.evidence
                if effect_claim is not None
                and (pending_checker_unmet or effect_claim.value == "unmet")
                else control_state.evidence if control_state is not None else ""
            )
        elif (
            effect_claim is not None and effect_claim.value == "confirmed"
        ) or (control_state is not None and control_state.value == "confirmed"):
            effect_status = "satisfied"
            effect_evidence = (
                effect_claim.evidence
                if effect_claim is not None and effect_claim.value == "confirmed"
                else control_state.evidence if control_state is not None else ""
            )
        else:
            effect_status = "unknown"
            effect_evidence = ""
        write_confirmed = bool(write is not None and write.value == "confirmed")
        effect_authoritative = bool(
            (effect_claim is not None and effect_claim.authoritative)
            or (control_state is not None and control_state.authoritative)
        )

        commit_confirmed = bool(
            execution is not None
            and execution.value == "confirmed"
            and execution.source_type == "runtime.commit_dispatch"
        )
        if commit_confirmed:
            persistence_status = "submitted"
        elif write_confirmed and contract.persistence == "explicit_commit":
            persistence_status = "pending"
        else:
            persistence_status = "clean"
        persistence = persistence_assessment or PersistenceAssessment(
            status=persistence_status,
            orphan_commit=commit_confirmed and not write_confirmed,
        )

        if action_delivery == "failed":
            return CompletionEvaluation(
                "contradicted", "动作派发已被明确证伪", "failed",
                next="recover",
            )
        if action_targeting == "contradicted" and effect_status != "satisfied":
            return CompletionEvaluation(
                "contradicted", "动作命中了错误目标", "failed",
                next="recover",
            )
        if effect_status == "contradicted":
            return CompletionEvaluation(
                "contradicted",
                effect_evidence or "动作后的业务状态已被明确证伪",
                "failed",
                next="recover",
            )

        if contract.completion_mode == "filter_state_with_result":
            if filter_state is not None and filter_state.value == "confirmed":
                # A zero-row result is a valid return value.  The following interpreter branch,
                # not this statement, decides whether to run a fallback search.
                return CompletionEvaluation(
                    status="satisfied",
                    completion_status="confirmed",
                    reason=filter_state.evidence or "目标筛选状态已权威确认",
                    next="complete",
                )
            return CompletionEvaluation("pending", "筛选状态尚未权威确认", next="act")

        if contract.completion_mode == "filter_state":
            if filter_state is not None and filter_state.value == "confirmed":
                return CompletionEvaluation(
                    "satisfied",
                    filter_state.evidence or "目标筛选状态已权威确认",
                    "confirmed",
                    next="complete",
                )
            return CompletionEvaluation("pending", "筛选状态尚未权威确认", next="act")

        if contract.completion_mode == "arrival":
            if not (action_delivery == "delivered" and effect_status == "satisfied"):
                return CompletionEvaluation(
                    "pending", "导航动作或目标页面确认尚不完整", next="act"
                )
            return CompletionEvaluation(
                "satisfied", effect_claim.evidence if effect_claim else "目标页面状态已确认",
                "confirmed", next="complete",
            )

        if contract.completion_mode == "read":
            if (
                collection_coverage is not None
                and collection_coverage.value == "complete"
                and collection_coverage.authoritative
            ):
                return CompletionEvaluation(
                    "satisfied",
                    collection_coverage.evidence or "集合遍历已完成",
                    "confirmed",
                    next="complete",
                )
            return CompletionEvaluation(
                "pending", "集合遍历尚未达到可验证边界", next="act"
            )

        if contract.completion_mode == "mutation":
            if (
                contract.effect_mode == "dispatch"
                and action_delivery == "delivered"
                and (
                    contract.persistence == "immediate"
                    or persistence.terminal_turn is not None
                )
            ):
                return CompletionEvaluation(
                    "satisfied",
                    "声明的副作用动作已可靠派发，业务效果没有独立反馈通道",
                    "accepted_unverified",
                    next="complete",
                )
            if effect_status == "unmet":
                if persistence.status == "pending":
                    return CompletionEvaluation(
                        "pending",
                        effect_evidence or "目标状态尚未越过持久化边界",
                        next=(
                            "observe"
                            if persistence.terminal_turn is not None
                            else "commit" if persistence.terminal_ready else "act"
                        ),
                    )
                if (
                    persistence.status == "submitted"
                    and effect_authoritative
                ):
                    return CompletionEvaluation(
                        "contradicted",
                        effect_evidence or "提交后的业务状态仍未达到声明目标",
                        "failed",
                        next="recover",
                    )
                if persistence.status == "submitted":
                    return CompletionEvaluation(
                        "satisfied",
                        "终端提交已可靠派发，非权威验收尚未确认目标状态",
                        "accepted_unverified",
                        next="complete",
                    )
                return CompletionEvaluation(
                    "pending", effect_evidence or "目标字段尚未达到声明值",
                    next="act",
                )
            if (
                effect_status == "satisfied"
                and effect_authoritative
                and contract.persistence == "explicit_commit"
                and persistence.status == "clean"
                and contract.effect_mode == "transform"
            ):
                return CompletionEvaluation(
                    "pending",
                    "声明的业务状态已确认，等待本次调用越过显式持久化边界",
                    next="commit",
                )
            if (
                persistence.orphan_commit
                and persistence.status == "submitted"
                and contract.effect_mode != "dispatch"
                and not (effect_status == "satisfied" and effect_authoritative)
            ):
                return CompletionEvaluation(
                    "contradicted",
                    "提交已派发，但当前执行作用域没有目标写入",
                    "failed",
                    next="recover",
                )
            if persistence.status == "pending":
                if (
                    persistence.terminal_turn is not None
                    and effect_status == "satisfied"
                ):
                    return CompletionEvaluation(
                        "satisfied",
                        effect_evidence or "提交后的业务状态已确认",
                        "confirmed",
                        next="complete",
                    )
                return CompletionEvaluation(
                    "pending",
                    (
                        "终端提交已派发，等待持久化响应或业务终态确认"
                        if persistence.terminal_turn is not None
                        else "目标写入已完成，但显式持久化边界尚未提交"
                    ),
                    next=(
                        "observe"
                        if persistence.terminal_turn is not None
                        else "commit" if persistence.terminal_ready else "act"
                    ),
                )
            if persistence.status == "submitted":
                if effect_status == "satisfied":
                    return CompletionEvaluation(
                        "satisfied",
                        effect_evidence or "业务状态及终端提交已确认",
                        "confirmed",
                        next="complete",
                    )
                return CompletionEvaluation(
                    "satisfied",
                    "终端提交已可靠派发，完成帧未出现反证",
                    "accepted_unverified",
                    next="complete",
                )
            if contract.effect_mode == "ensure" and effect_status == "satisfied":
                return CompletionEvaluation(
                    "satisfied", effect_evidence or "幂等目标状态已满足",
                    "confirmed", next="complete",
                )
            return CompletionEvaluation(
                "pending", "动作结果尚未形成可完成的业务状态",
                next="act",
            )

        if effect_status == "satisfied":
            return CompletionEvaluation(
                "satisfied", effect_evidence or "验收状态已确认", "confirmed",
                next="complete",
            )
        return CompletionEvaluation(
            "pending", "当前证据不足以完成执行单元",
            next="act",
        )


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
