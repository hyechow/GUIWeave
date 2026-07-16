"""Platform-neutral evidence fusion for interactive statement execution.

The supervisor receives several independent signals: an event may have crossed the GUI
boundary, the page may have responded, and the requested postcondition may or may not be
confirmed.  This module keeps those axes separate and gives deterministic adapter claims
precedence only inside the domains for which they are authoritative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from gui_agent.core.schemas import StatementContract, PersistenceMode
from gui_agent.core.run.persistence import PersistenceAssessment
CompletionMode = Literal[
    "arrival",
    "filter_state",
    "filter_state_with_result",
    "state",
    "command",
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
    persistence: PersistenceMode = "immediate"

    @classmethod
    def from_statement(cls, statement: StatementContract) -> "ExecutionContract":
        if statement.kind == "navigation":
            mode: CompletionMode = "arrival"
        elif statement.kind == "filter":
            mode = "filter_state_with_result" if statement.returns else "filter_state"
        elif statement.kind == "action":
            mode = "state" if statement.target_values else "command"
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
            persistence=statement.persistence,
        )


@dataclass(frozen=True)
class EvidenceClaim:
    """One typed assertion from an adapter, runtime ledger, or Transition."""

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
    """Runtime's evidence-only answer to a model completion proposal."""

    status: CompletionStatus
    reason: str
    completion_status: Literal[
        "confirmed", "accepted_unverified", "failed", "in_progress"
    ] = "in_progress"


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


class CompletionReducer:
    """Reduce action, effect, and persistence evidence for terminal validation.

    The three assessments remain independent.  In particular, dispatch does not prove the
    business effect and a visible draft value does not prove persistence.

    This reducer has no route vocabulary and cannot choose the next action. Semantic flow belongs
    to the LLM; this component answers only whether cited facts support a terminal outcome.
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
        if contract.completion_mode == "state":
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
            if covers_expected_subject(item)
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

        # An authoritative reading of the declared controls can disprove a non-authoritative
        # semantic diagnosis. This precedence cannot prove dispatch or persistence.
        outcome_contradicted = bool(
            effect_claim is not None
            and effect_claim.value == "contradicted"
            and action_delivery == "delivered"
        )
        pending_semantic_unmet = bool(
            outcome_contradicted
            and effect_claim is not None
            and not effect_claim.authoritative
            and effect_claim.source_type != "transition.rejected"
            and persistence_assessment is not None
            and persistence_assessment.status == "pending"
        )
        if pending_semantic_unmet:
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
        elif pending_semantic_unmet or (
            effect_claim is not None and effect_claim.value == "unmet"
        ) or (
            control_state is not None
            and control_state.value in {"contradicted", "unmet"}
        ):
            effect_status = "unmet"
            effect_evidence = (
                effect_claim.evidence
                if effect_claim is not None
                and (pending_semantic_unmet or effect_claim.value == "unmet")
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
            )
        if action_targeting == "contradicted" and effect_status != "satisfied":
            return CompletionEvaluation(
                "contradicted", "动作命中了错误目标", "failed",
            )
        if effect_status == "contradicted":
            return CompletionEvaluation(
                "contradicted",
                effect_evidence or "动作后的业务状态已被明确证伪",
                "failed",
            )

        if contract.completion_mode == "filter_state_with_result":
            if filter_state is not None and filter_state.value == "confirmed":
                # A zero-row result is a valid return value.  The following interpreter branch,
                # not this statement, decides whether to run a fallback search.
                return CompletionEvaluation(
                    status="satisfied",
                    completion_status="confirmed",
                    reason=filter_state.evidence or "目标筛选状态已权威确认",
                )
            return CompletionEvaluation("pending", "筛选状态尚未权威确认")

        if contract.completion_mode == "filter_state":
            if filter_state is not None and filter_state.value == "confirmed":
                return CompletionEvaluation(
                    "satisfied",
                    filter_state.evidence or "目标筛选状态已权威确认",
                    "confirmed",
                )
            return CompletionEvaluation("pending", "筛选状态尚未权威确认")

        if contract.completion_mode == "arrival":
            if effect_status != "satisfied":
                return CompletionEvaluation(
                    "pending", "目标页面确认尚不完整"
                )
            return CompletionEvaluation(
                "satisfied", effect_claim.evidence if effect_claim else "目标页面状态已确认",
                "confirmed" if effect_authoritative else "accepted_unverified",
            )

        if contract.completion_mode == "read":
            if (
                collection_coverage is not None
                and collection_coverage.value == "complete"
            ):
                return CompletionEvaluation(
                    "satisfied",
                    collection_coverage.evidence or "集合遍历已完成",
                    (
                        "confirmed"
                        if collection_coverage.authoritative
                        else "accepted_unverified"
                    ),
                )
            return CompletionEvaluation(
                "pending", "集合遍历尚未达到可验证边界"
            )

        if contract.completion_mode == "state":
            if effect_status == "unmet":
                if persistence.status == "pending":
                    return CompletionEvaluation(
                        "pending",
                        effect_evidence or "目标状态尚未越过持久化边界",
                    )
                if (
                    persistence.status == "submitted"
                    and effect_authoritative
                ):
                    return CompletionEvaluation(
                        "contradicted",
                        effect_evidence or "提交后的业务状态仍未达到声明目标",
                        "failed",
                    )
                if persistence.status == "submitted":
                    return CompletionEvaluation(
                        "satisfied",
                        "终端提交已可靠派发，非权威验收尚未确认目标状态",
                        "accepted_unverified",
                    )
                return CompletionEvaluation(
                    "pending", effect_evidence or "目标字段尚未达到声明值",
                )
            if (
                persistence.orphan_commit
                and not (effect_status == "satisfied" and effect_authoritative)
            ):
                return CompletionEvaluation(
                    "contradicted",
                    "提交已派发，但当前执行作用域没有目标写入",
                    "failed",
                )
            if (
                effect_status == "satisfied"
                and effect_authoritative
                and not write_confirmed
            ):
                return CompletionEvaluation(
                    "satisfied",
                    effect_evidence or "幂等目标状态已满足",
                    "confirmed",
                )
            if persistence.status == "pending":
                if (
                    persistence.terminal_turn is not None
                    and effect_status == "satisfied"
                ):
                    return CompletionEvaluation(
                        "satisfied",
                        effect_evidence or "提交后的业务状态已确认",
                        (
                            "confirmed"
                            if effect_authoritative
                            else "accepted_unverified"
                        ),
                    )
                return CompletionEvaluation(
                    "pending",
                    (
                        "终端提交已派发，等待持久化响应或业务终态确认"
                        if persistence.terminal_turn is not None
                        else "目标写入已完成，但显式持久化边界尚未提交"
                    ),
                )
            if persistence.status == "submitted":
                if effect_status == "satisfied":
                    return CompletionEvaluation(
                        "satisfied",
                        effect_evidence or "业务状态及终端提交已确认",
                        "confirmed" if effect_authoritative else "accepted_unverified",
                    )
                return CompletionEvaluation(
                    "satisfied",
                    "终端提交已可靠派发，完成帧未出现反证",
                    "accepted_unverified",
                )
            if effect_status == "satisfied":
                if not effect_authoritative and not write_confirmed:
                    return CompletionEvaluation(
                        "pending",
                        "声明目标状态只有模型判断，尚无结构化观察或本调用写入回执",
                    )
                return CompletionEvaluation(
                    "satisfied",
                    effect_evidence or "目标状态已满足",
                    "confirmed" if effect_authoritative else "accepted_unverified",
                )
            return CompletionEvaluation(
                "pending", "结构化目标状态尚未满足",
            )

        if contract.completion_mode == "command":
            if effect_status == "satisfied":
                return CompletionEvaluation(
                    "satisfied",
                    effect_evidence or "命令结果已观察确认",
                    "confirmed" if effect_authoritative else "accepted_unverified",
                )
            if commit_confirmed and (
                contract.persistence == "immediate"
                or persistence.terminal_turn is not None
            ):
                return CompletionEvaluation(
                    "satisfied",
                    "命令动作已可靠派发，当前没有稳定的业务反馈通道",
                    "accepted_unverified",
                )
            return CompletionEvaluation(
                "pending", "命令尚无可观察结果或可靠派发回执",
            )

        if effect_status == "satisfied":
            return CompletionEvaluation(
                "satisfied",
                effect_evidence or "验收状态已确认",
                "confirmed" if effect_authoritative else "accepted_unverified",
            )
        return CompletionEvaluation(
            "pending", "当前证据不足以完成执行单元",
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
