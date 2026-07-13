"""Evidence producers for milestone execution.

These functions translate observations and action receipts into typed claims.  They do not
advance, retry, fail, or otherwise own execution control flow.
"""

from __future__ import annotations

from gui_agent.core.run.action_ledger import ActionLedger
from gui_agent.core.run.execution_signals import (
    EvidenceClaim,
    ExecutionContract,
    claim,
    target_matches_declared,
)
from gui_agent.core.run.progress_monitor import ProgressMonitor
from gui_agent.core.run.mutation import resolve_mutation
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn

from .action_protocol import PersistenceBoundaryState, is_commit_turn
from .observation_state import (
    RuntimeFilterIntent,
    filter_chips_clean,
    filter_state_satisfies_target,
    observed_filter_intent,
)
from .schemas import _SingleCheckResult


def execution_contract_for(
    milestone: Milestone,
    configured: ExecutionContract | None,
) -> ExecutionContract:
    """Return the configured contract or derive it from the milestone."""
    contract = configured
    if contract is None or contract.statement_id != milestone.id:
        contract = ExecutionContract.from_milestone(milestone)
    return contract


def action_lifecycle_claims(
    milestone: Milestone,
    history: list[PolicyTurn],
    *,
    scope: str,
    monitor: ProgressMonitor,
    ledger: ActionLedger,
    boundary: PersistenceBoundaryState | None = None,
) -> list[EvidenceClaim]:
    """Translate persisted action lifecycle state into typed evidence claims."""
    claims: list[EvidenceClaim] = []
    scoped_history = [
        turn
        for turn in history
        if getattr(turn.supervisor, "execution_scope", "") in {"", scope}
    ]
    if (
        history
        and history[-1].supervisor is not None
        and history[-1].supervisor.milestone_id == milestone.id
        and history[-1].executed
        and history[-1] not in scoped_history
    ):
        scoped_history.append(history[-1])
    latest = ledger.latest_dispatched(scoped_history, milestone.id)
    if latest is None:
        return claims
    lifecycle_scope = getattr(latest.supervisor, "execution_scope", "") or scope
    if monitor.url_changed:
        claims.append(claim(
            "page.response",
            "confirmed",
            source_type="runtime.effect_monitor",
            scope=scope,
            subject_scope=lifecycle_scope,
            evidence="URL changed after the previous action",
            authoritative=True,
        ))
    terminal = (
        boundary.is_terminal_dispatch(latest, milestone)
        if boundary is not None
        else is_commit_turn(latest, milestone)
    )
    claims.append(claim(
        "action.execution",
        "confirmed",
        source_type=("runtime.commit_dispatch" if terminal else "runtime.action_dispatch"),
        scope=scope,
        subject_scope=lifecycle_scope,
        evidence="runtime action ledger records a dispatched event",
        authoritative=True,
    ))
    write_turn = ledger.latest_write(history, milestone.id, scope=lifecycle_scope)
    if milestone.kind == "action" and milestone.target_values:
        write_turn = next(
            (
                turn
                for turn in reversed(history)
                if turn.action_signal is not None
                and (receipt := turn.action_signal.mutation_receipt) is not None
                and receipt.statement_id == milestone.id
            ),
            None,
        )
    if (
        write_turn is None
        and terminal
        and not milestone.requires_commit
        and not (milestone.kind == "action" and milestone.target_values)
        and latest.action_signal is not None
        and target_matches_declared(
            latest.action_signal.target_control
            or getattr(latest.supervisor, "target_control", ""),
            (
                *(milestone.target_controls or []),
                *(milestone.target_values or {}).keys(),
            ),
        )
    ):
        write_turn = latest
    if write_turn is not None:
        claims.append(claim(
            "action.write",
            "confirmed",
            source_type="runtime.write_dispatch",
            scope=scope,
            subject_scope=(
                getattr(write_turn.supervisor, "execution_scope", "") or lifecycle_scope
            ),
            evidence="runtime action ledger records an on-target target write",
            authoritative=True,
        ))
    target_verify = latest.target_verify
    if target_verify is not None:
        claims.append(claim(
            "action.target",
            "confirmed" if target_verify.on_target else "contradicted",
            source_type="runtime.target_verify",
            scope=scope,
            subject_scope=lifecycle_scope,
            evidence=(
                "action target verified"
                if target_verify.on_target
                else f"off target: {target_verify.actual_element}"
            ),
            authoritative=True,
        ))
    return claims


def target_value_claims(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    scope: str,
) -> list[EvidenceClaim]:
    """Translate declared field/value matches into authoritative state claims."""
    if milestone.kind != "action" or not milestone.target_values:
        return []
    state = resolve_mutation(milestone, observation, history)
    claims: list[EvidenceClaim] = []
    if state.status == "complete":
        claims.append(claim(
            "control.state",
            "confirmed",
            source_type="obs.mutation.desired_state",
            scope=scope,
            subject_scope=state.subject_ref or scope,
            evidence=state.evidence,
            authoritative=True,
            coverage="resolved_subject",
        ))
    elif state.status in {"preparing", "writable", "absent", "ambiguous"}:
        claims.append(claim(
            "control.state",
            "contradicted",
            source_type="obs.mutation.desired_state",
            scope=scope,
            subject_scope=state.subject_ref or scope,
            evidence=state.evidence,
            authoritative=True,
            coverage="resolved_subject",
        ))
    return claims


def checker_claim(
    check: _SingleCheckResult,
    *,
    scope: str,
    subject_scope: str = "",
) -> EvidenceClaim:
    """Translate a probabilistic checker result without granting it control-flow authority."""
    if check.outcome_status == "contradicted":
        value = "contradicted"
    elif check.outcome_status == "confirmed":
        value = "confirmed"
    else:
        value = "unverified"
    return claim(
        "business.outcome",
        value,
        source_type="checker",
        scope=scope,
        subject_scope=subject_scope or scope,
        evidence=check.reason or check.summary,
        coverage="visible_frame",
    )


def runtime_filter_intent(
    milestone: Milestone,
    history: list[PolicyTurn],
    *,
    scope: str,
    ledger: ActionLedger,
) -> RuntimeFilterIntent | None:
    """Derive the concrete control/value used by the current filter attempt.

    Prefer a write receipt.  A filter may also begin with its desired control already populated;
    in that case the first action is a commit (Apply/Search/Enter), and the dispatched commit's
    structured supervisor target is the only concrete attempt identity.
    """
    if milestone.kind != "filter":
        return None
    turn = ledger.latest_write(history, milestone.id, scope=scope)
    if turn is None:
        turn = ledger.latest_commit(history, milestone.id, scope=scope)
    if turn is None or turn.action_signal is None:
        return None
    signal = turn.action_signal
    value = signal.target_value
    if not value and turn.supervisor is not None:
        value = str(getattr(turn.supervisor, "target_value", "") or "")
    if not value and turn.action_decision is not None:
        value = str(getattr(turn.action_decision.action, "text", "") or "")
    control = signal.target_control or getattr(turn.supervisor, "target_control", "")
    if not control or not value:
        return None
    return RuntimeFilterIntent(control, value)


def resolved_filter_intent(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    scope: str,
    ledger: ActionLedger,
) -> RuntimeFilterIntent | None:
    """Resolve one concrete filter identity from current state and dispatch receipts."""
    runtime_intent = runtime_filter_intent(
        milestone, history, scope=scope, ledger=ledger
    )
    return observed_filter_intent(
        getattr(observation, "applied_filters", None),
        getattr(observation, "form_controls", None),
        milestone,
        runtime_intent,
    ) or runtime_intent


def observation_state_claims(
    milestone: Milestone,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    scope: str,
    ledger: ActionLedger,
) -> list[EvidenceClaim]:
    """Build deterministic claims from the current adapter observation."""
    claims = target_value_claims(milestone, observation, history, scope=scope)
    applied_filters = getattr(observation, "applied_filters", None)
    filter_intent = resolved_filter_intent(
        milestone, observation, history, scope=scope, ledger=ledger
    )
    filter_applied = bool(
        milestone.kind == "filter"
        and filter_state_satisfies_target(applied_filters, milestone, filter_intent)
        and filter_chips_clean(applied_filters, milestone, filter_intent)
    )
    if filter_applied:
        claims.append(claim(
            "filter.state",
            "confirmed",
            source_type="obs.applied_filters",
            scope=scope,
            subject_scope=scope,
            evidence=f"target filter is active: {applied_filters}",
            authoritative=True,
            coverage="declared_filter_state",
        ))
    return claims


__all__ = [
    "action_lifecycle_claims",
    "checker_claim",
    "execution_contract_for",
    "observation_state_claims",
    "resolved_filter_intent",
    "runtime_filter_intent",
    "target_value_claims",
]
