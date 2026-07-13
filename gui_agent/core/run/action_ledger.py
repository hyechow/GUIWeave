"""Run-scoped action lifecycle ledger and pre-dispatch commit guard."""

from __future__ import annotations

from typing import Any, Iterable

from gui_agent.core.schemas import PolicyTurn, SupervisorStep

from .progress_monitor import action_signature


_COMMIT_CAPABLE_ACTION_TYPES = frozenset({"tap", "click", "press_enter"})
_ITERATIVE_ACTION_TYPES = frozenset({"scroll", "drag"})
_WRITE_ACTION_TYPES = frozenset({"type", "clear_text", "select_option"})


def effective_action_role(supervisor_step: SupervisorStep, action: Any) -> str:
    """Resolve lifecycle role from the concrete action crossing the GUI boundary.

    The planner's role is advisory until action policy has produced a concrete
    primitive. A scroll or drag cannot consume a milestone's at-most-once commit
    slot even when an LLM mislabeled it; other non-dispatch primitives are
    preparation actions.
    """
    action_type = str(getattr(action, "action_type", "") or "").lower()
    if action_type in _ITERATIVE_ACTION_TYPES:
        return "iterate"
    if action_type in _WRITE_ACTION_TYPES:
        return "write"
    if action_type not in _COMMIT_CAPABLE_ACTION_TYPES:
        return "prepare"
    return supervisor_step.atomic_role or "prepare"


def semantic_action_key(supervisor_step: SupervisorStep, action: Any) -> str:
    """Return a stable action identity within one milestone execution scope."""
    scope = supervisor_step.execution_scope or ""
    milestone_id = supervisor_step.milestone_id or ""
    role = effective_action_role(supervisor_step, action)
    prefix = f"{scope}|{milestone_id}|{role}"
    # A commit is the persistence boundary of its interactive Run. Coordinates and button
    # wording must not create a second key for the same side effect.
    if role == "commit":
        return prefix
    group_id = supervisor_step.target_group_id or ""
    group_part = f"|group:{group_id}" if group_id else ""
    return f"{prefix}{group_part}|{action_signature(action)}"


class ActionLedger:
    """Read-only queries over persisted action lifecycle records.

    Dispatch authorization belongs to the milestone controller.  The ledger records what
    happened; it never suppresses an action or decides whether a statement may continue.
    """

    @staticmethod
    def latest_dispatched(
        history: Iterable[PolicyTurn], milestone_id: str
    ) -> PolicyTurn | None:
        """Return the latest structured dispatch for a milestone."""
        return next(
            (
                turn
                for turn in reversed(list(history))
                if turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
            ),
            None,
        )

    @staticmethod
    def latest_pending(
        history: Iterable[PolicyTurn], milestone_id: str
    ) -> PolicyTurn | None:
        """Return the newest unresolved dispatch; closed outcomes cannot absorb later checks."""
        return next(
            (
                turn
                for turn in reversed(list(history))
                if turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
                and turn.action_signal.outcome == "unverified"
            ),
            None,
        )

    @staticmethod
    def latest_write(
        history: Iterable[PolicyTurn], milestone_id: str, *, scope: str = ""
    ) -> PolicyTurn | None:
        """Return the newest on-target write in this interactive statement."""
        return next(
            (
                turn
                for turn in reversed(list(history))
                if turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
                and turn.action_signal.role == "write"
                and turn.action_signal.target != "off_target"
                and (
                    not scope
                    or getattr(turn.supervisor, "execution_scope", "") in {"", scope}
                )
            ),
            None,
        )

    @staticmethod
    def latest_commit(
        history: Iterable[PolicyTurn], milestone_id: str, *, scope: str = ""
    ) -> PolicyTurn | None:
        """Return the newest dispatched commit in this interactive statement."""
        return next(
            (
                turn
                for turn in reversed(list(history))
                if turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
                and turn.action_signal.role == "commit"
                and (
                    not scope
                    or getattr(turn.supervisor, "execution_scope", "") in {"", scope}
                )
            ),
            None,
        )
