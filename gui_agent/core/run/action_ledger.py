"""Run-scoped action lifecycle ledger and pre-dispatch commit guard."""

from __future__ import annotations

from typing import Any, Iterable

from gui_agent.core.schemas import PolicyTurn, SupervisorStep

from .progress_monitor import action_signature


def semantic_action_key(supervisor_step: SupervisorStep, action: Any) -> str:
    """Return a stable action identity within one milestone execution scope."""
    scope = supervisor_step.execution_scope or ""
    milestone_id = supervisor_step.milestone_id or ""
    role = supervisor_step.atomic_role or "prepare"
    prefix = f"{scope}|{milestone_id}|{role}"
    # A commit is the persistence boundary of its interactive Run. Coordinates and button
    # wording must not create a second key for the same side effect.
    return prefix if role == "commit" else f"{prefix}|{action_signature(action)}"


class ActionLedger:
    """Query persisted turns as the source of truth for action dispatch lifecycle."""

    def authorize(
        self,
        supervisor_step: SupervisorStep,
        action_decision: Any,
        history: Iterable[PolicyTurn],
    ) -> tuple[bool, str, str]:
        """Apply an at-most-once guard before a commit crosses the GUI boundary."""
        action = getattr(action_decision, "action", None)
        if action is None:
            return True, "", ""
        key = semantic_action_key(supervisor_step, action)
        if supervisor_step.atomic_role != "commit":
            return True, key, ""

        turns = list(history)
        matching = [
            turn
            for turn in turns
            if turn.action_signal is not None
            and turn.action_signal.action_key == key
            and turn.action_signal.execution == "dispatched"
            and turn.action_signal.target != "off_target"
        ]
        if not matching:
            return True, key, ""

        latest = matching[-1]
        signal = latest.action_signal
        assert signal is not None
        if signal.outcome != "contradicted":
            return False, key, "commit 已可靠派发，结果尚未证伪，禁止原样重复副作用"

        corrected = any(
            turn.index > latest.index
            and turn.action_signal is not None
            and turn.action_signal.role == "prepare"
            and turn.action_signal.execution == "dispatched"
            and getattr(turn.supervisor, "execution_scope", "")
            == supervisor_step.execution_scope
            for turn in turns
        )
        if corrected:
            return True, key, ""
        return False, key, "上次 commit 已被明确拒绝，但尚未执行修正输入，禁止重复提交"

    @staticmethod
    def latest_dispatched(
        history: Iterable[PolicyTurn], milestone_id: str
    ) -> PolicyTurn | None:
        """Return the latest dispatch for a milestone, including legacy persisted turns."""
        turns = list(history)
        structured = next(
            (
                turn
                for turn in reversed(turns)
                if turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
            ),
            None,
        )
        if structured is not None:
            return structured
        return next(
            (
                turn
                for turn in reversed(turns)
                if turn.executed
                and turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
            ),
            None,
        )
