"""Evidence producers for statement execution.

These functions translate observations and action receipts into typed claims.  They do not
advance, retry, fail, or otherwise own execution control flow.
"""

from __future__ import annotations

from gui_agent.core.run.action_signals import latest_action
from gui_agent.core.run.execution_signals import (
    EvidenceClaim,
    ExecutionContract,
    claim,
    target_matches_declared,
)
from gui_agent.core.run.mutation import resolve_mutation
from gui_agent.core.run.persistence import assess_persistence
from gui_agent.core.schemas import EffectSignal, StatementContract, Observation, PolicyTurn

from .observation_state import (
    RuntimeFilterIntent,
    filter_chips_clean,
    filter_state_satisfies_target,
    observed_filter_intent,
)
from .schemas import _StatementTransitionResult


def action_lifecycle_claims(
    statement: StatementContract,
    history: list[PolicyTurn],
    *,
    scope: str,
) -> list[EvidenceClaim]:
    """Translate persisted action lifecycle state into typed evidence claims."""
    claims: list[EvidenceClaim] = []
    persisted_effect = next(
        (
            turn.effect_signal
            for turn in reversed(history)
            if turn.effect_signal is not None
            and turn.effect_signal.statement_id == statement.id
            and turn.effect_signal.authoritative
            and turn.effect_signal.status != "unknown"
        ),
        None,
    )
    if persisted_effect is not None:
        effect_value = {
            "satisfied": "confirmed",
            "unmet": "unmet",
            "contradicted": "contradicted",
        }[persisted_effect.status]
        claims.append(claim(
            "effect.state",
            effect_value,
            source_type=f"journal.{persisted_effect.source_type}",
            scope=scope,
            subject_scope=persisted_effect.subject_ref,
            evidence="; ".join(persisted_effect.evidence),
            authoritative=True,
        ))
    latest = latest_action(history, statement.id)
    if latest is None:
        return claims
    lifecycle_scope = getattr(latest.supervisor, "execution_scope", "") or scope
    persistence = assess_persistence(
        statement,
        history,
        scope=lifecycle_scope,
    )
    signal = latest.action_signal
    if signal is not None and signal.response == "observed":
        channels = set(signal.response_channels)
        claims.append(claim(
            "page.response",
            "confirmed",
            source_type="runtime.action_response",
            scope=scope,
            subject_scope=lifecycle_scope,
            evidence=(
                "URL changed after the previous action"
                if "url" in channels
                else "structured page state changed after the previous action"
            ),
            authoritative=True,
            coverage=(
                "navigation_transition"
                if "url" in channels
                else "in_place_transition"
            ),
        ))
    terminal = bool(
        persistence.terminal_turn is not None
        and latest.index == persistence.terminal_turn.index
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
    write_turn = persistence.latest_write
    if (
        write_turn is None
        and terminal
        and statement.persistence == "immediate"
        and not (statement.kind == "action" and statement.target_values)
        and latest.action_signal is not None
        and target_matches_declared(
            latest.action_signal.target_control
            or (
                latest.supervisor.action_intent.target_control
                if latest.supervisor.action_intent is not None
                else ""
            ),
            (
                *(statement.target_controls or []),
                *(statement.target_values or {}).keys(),
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
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    scope: str,
) -> list[EvidenceClaim]:
    """Translate declared field/value matches into authoritative state claims."""
    if statement.kind != "action" or not statement.target_values:
        return []
    state = resolve_mutation(statement, observation, history)
    claims: list[EvidenceClaim] = []
    if state.status == "complete":
        claims.append(claim(
            "control.state",
            "confirmed",
            source_type="obs.mutation.desired_state",
            scope=scope,
            subject_scope=state.subject_ref,
            evidence=state.evidence,
            authoritative=True,
            coverage="resolved_subject",
        ))
    elif state.status in {"writable", "absent"}:
        claims.append(claim(
            "control.state",
            "unmet",
            source_type="obs.mutation.desired_state",
            scope=scope,
            subject_scope=state.subject_ref or scope,
            evidence=state.evidence,
            authoritative=True,
            coverage="resolved_subject",
        ))
    elif state.status == "ambiguous":
        claims.append(claim(
            "control.state",
            "unverified",
            source_type="obs.mutation.desired_state",
            scope=scope,
            subject_scope=state.subject_ref or scope,
            evidence=state.evidence,
            authoritative=True,
            coverage="ambiguous_subject",
        ))
    return claims


def observed_effect_signal(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
) -> EffectSignal | None:
    """Journal a positive target fact for this invocation; current observations still win."""
    if statement.kind != "action" or not statement.target_values:
        return None
    state = resolve_mutation(statement, observation, history)
    if state.status != "complete":
        return None
    return EffectSignal(
        statement_id=statement.id,
        status="satisfied",
        subject_ref=state.subject_ref,
        source_type="obs.mutation.desired_state",
        authoritative=True,
        evidence=[state.evidence] if state.evidence else [],
    )


def transition_claim(
    decision: _StatementTransitionResult,
    *,
    scope: str,
) -> EvidenceClaim:
    """Translate a model completion proposal without granting it fact authority."""
    evidence = "; ".join(
        f"{item.event_ref + ': ' if item.event_ref else ''}{item.claim}"
        for item in decision.evidence
    )
    return claim(
        "effect.state",
        "confirmed",
        source_type="transition",
        scope=scope,
        # A semantic completion proposal does not establish a mutation subject identity. Leaving
        # it unbound lets persistence receipts own subject provenance across redirects.
        subject_scope="",
        evidence=evidence or decision.reason or decision.summary,
        coverage="visible_frame",
    )


def runtime_filter_intent(
    statement: StatementContract,
    history: list[PolicyTurn],
    *,
    scope: str,
) -> RuntimeFilterIntent | None:
    """Derive the concrete control/value used by the current filter attempt.

    Prefer a write receipt.  A filter may also begin with its desired control already populated;
    in that case the first action is a commit (Apply/Search/Enter), and the dispatched commit's
    structured supervisor target is the only concrete attempt identity.
    """
    if statement.kind != "filter":
        return None
    turn = latest_action(history, statement.id, scope=scope, role="write")
    if turn is None:
        turn = latest_action(history, statement.id, scope=scope, role="commit")
    if turn is None or turn.action_signal is None:
        return None
    signal = turn.action_signal
    value = signal.target_value
    intent = turn.supervisor.action_intent if turn.supervisor is not None else None
    if not value and intent is not None:
        value = intent.target_value
    if not value and turn.action_decision is not None:
        value = str(getattr(turn.action_decision.action, "text", "") or "")
    control = signal.target_control or (
        intent.target_control if intent is not None else ""
    )
    if not control or not value:
        return None
    return RuntimeFilterIntent(control, value)


def resolved_filter_intent(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    scope: str,
) -> RuntimeFilterIntent | None:
    """Resolve one concrete filter identity from current state and dispatch receipts."""
    runtime_intent = runtime_filter_intent(
        statement, history, scope=scope
    )
    return observed_filter_intent(
        getattr(observation, "applied_filters", None),
        getattr(observation, "form_controls", None),
        statement,
        runtime_intent,
    ) or runtime_intent


def observation_state_claims(
    statement: StatementContract,
    observation: Observation,
    history: list[PolicyTurn],
    *,
    scope: str,
) -> list[EvidenceClaim]:
    """Build deterministic claims from the current adapter observation."""
    claims = target_value_claims(
        statement,
        observation,
        history,
        scope=scope,
    )
    applied_filters = getattr(observation, "applied_filters", None)
    filter_intent = resolved_filter_intent(
        statement, observation, history, scope=scope
    )
    filter_applied = bool(
        statement.kind == "filter"
        and filter_state_satisfies_target(applied_filters, statement, filter_intent)
        and filter_chips_clean(applied_filters, statement, filter_intent)
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
    "observed_effect_signal",
    "observation_state_claims",
    "resolved_filter_intent",
    "runtime_filter_intent",
    "target_value_claims",
    "transition_claim",
]
