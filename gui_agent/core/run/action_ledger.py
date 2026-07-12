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
        if str(getattr(action, "action_type", "") or "").lower() == "stop":
            # Completion belongs to the milestone supervisor. An action-policy ``stop`` emitted
            # while the supervisor is still asking for work is only a no-op opinion; dispatching
            # it as success creates a contradictory turn (checker=in_progress, action=done).
            return False, key, "action policy 无权用 stop 完成仍处于 in_progress 的 milestone"
        role = effective_action_role(supervisor_step, action)
        turns = list(history)
        if role == "write":
            dispatched = [
                turn
                for turn in turns
                if turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
            ]
            latest = dispatched[-1] if dispatched else None
            if (
                latest is not None
                and latest.action_signal is not None
                and latest.action_signal.action_key == key
            ):
                return (
                    False,
                    key,
                    "同一结构目标已派发过相同写入，期间没有其它有效动作；"
                    "禁止原样重写，必须重新读取目标状态或更换操作路径",
                )
            return True, key, ""
        if role != "commit":
            return True, key, ""
        if supervisor_step.milestone_kind not in {None, "action"}:
            # Navigation/filter commits update reversible UI state. At-most-once protects only
            # persistent business mutations; loop/stuck detection owns ineffective UI retries.
            return True, key, ""

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
            and (
                turn.action_signal.role == "write"
                or str(
                    getattr(
                        getattr(turn.action_decision, "action", None),
                        "action_type",
                        "",
                    )
                ).lower()
                in _WRITE_ACTION_TYPES
            )
            and turn.action_signal.execution == "dispatched"
            and getattr(turn.supervisor, "execution_scope", "")
            == supervisor_step.execution_scope
            for turn in turns
        )
        if corrected:
            return True, key, ""
        return False, key, "上次 commit 已被明确拒绝，但尚未执行新的目标写入，禁止重复提交"

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
        turns = list(history)
        structured = next(
            (
                turn
                for turn in reversed(turns)
                if turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and turn.action_signal is not None
                and turn.action_signal.execution == "dispatched"
                and turn.action_signal.role == "write"
                and turn.action_signal.target != "off_target"
                and (
                    not scope
                    or getattr(turn.supervisor, "execution_scope", "") == scope
                )
            ),
            None,
        )
        if structured is not None:
            return structured
        # Persisted contexts created before ActionSignal existed still carry the concrete
        # primitive and supervisor role. Accept only mechanically evident writes.
        return next(
            (
                turn
                for turn in reversed(turns)
                if turn.executed
                and turn.supervisor is not None
                and turn.supervisor.milestone_id == milestone_id
                and (
                    getattr(turn.supervisor, "atomic_role", "prepare") == "write"
                    or str(
                        getattr(
                            getattr(turn.action_decision, "action", None),
                            "action_type",
                            "",
                        )
                    ).lower()
                    in _WRITE_ACTION_TYPES
                )
                and (
                    not scope
                    or getattr(turn.supervisor, "execution_scope", "") in {"", scope}
                )
                and (
                    turn.target_verify is None or turn.target_verify.on_target
                )
            ),
            None,
        )
