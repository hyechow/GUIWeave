"""Typed four-layer state projected to the GUI Worker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from gui_agent.core.tool_agent.contracts import MaterializedFrame, WorkerSpec

from .reducer import WorkerMemoryView


@dataclass(frozen=True)
class GoalContractSnapshot:
    """Immutable Worker goal plus the currently selected execution approach."""

    profile: str
    goal: str
    success_criteria: tuple[str, ...]
    approach: str
    input_bindings: tuple[dict[str, Any], ...] = ()
    unresolved_inputs: dict[str, Any] = field(default_factory=dict)
    output_contract: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProgressSnapshot:
    """Bounded temporal reduction; raw receipts remain only in the Journal."""

    worker_id: str
    targets: dict[str, tuple[str, ...]]
    commitments: dict[str, tuple[str, ...]]
    effects: tuple[dict[str, str], ...]
    coverage: dict[str, Any]
    latest_transition_ref: str = ""
    audit_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransitionContract:
    """Mechanically admissible next-transition class, never task semantics."""

    mode: str
    allowed_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()
    required_direction: str = ""
    forbidden_directions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CurrentStateSnapshot:
    """Highest-salience state for the current frame or same-frame repair."""

    surface: dict[str, Any]
    observation: dict[str, Any]
    interaction: dict[str, Any]
    traversal: dict[str, Any]
    recovery: dict[str, Any] | None
    user_input: dict[str, Any]
    transition_contract: TransitionContract | None
    completion: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_goal_contract(spec: WorkerSpec) -> GoalContractSnapshot:
    dumped = spec.model_dump(mode="json")
    return GoalContractSnapshot(
        profile=spec.profile,
        goal=spec.goal,
        success_criteria=tuple(spec.success_criteria),
        approach=spec.strategy.approach,
        input_bindings=tuple(dumped.get("input_bindings") or ()),
        unresolved_inputs=dict(dumped.get("unresolved_inputs") or {}),
        output_contract={
            "data_requirements": dumped.get("data_requirements") or [],
        },
    )


def build_progress_snapshot(memory: WorkerMemoryView) -> ProgressSnapshot:
    transaction = memory.target_transaction
    active_targets = (transaction.target.ref,) if transaction is not None else ()
    observed_targets = (
        active_targets if transaction is not None and transaction.evidence else ()
    )
    completed = tuple(
        event.fact_ref for event in memory.completed_commitments
    )
    audit_events = (
        *memory.accumulated_evidence,
        *memory.active_commitments,
        *memory.completed_commitments,
        *memory.blocked_commitments,
        *memory.latest_gui_transition,
    )
    audit_refs = tuple(dict.fromkeys((
        *(event.event_ref for event in audit_events if event.event_ref),
        *(
            ref for event in memory.recent_receipts if event.receipt is not None
            for ref in event.receipt.commitment_refs
        ),
    )))[-16:]
    latest_transition_ref = next((
        event.event_ref for event in reversed(memory.latest_gui_transition)
        if event.event_ref
    ), "")
    return ProgressSnapshot(
        worker_id=memory.worker_id,
        targets={
            "observed": observed_targets,
            "active": active_targets,
            "blocked": tuple(event.fact_ref for event in memory.blocked_commitments),
        },
        commitments={
            "active": tuple(event.fact_ref for event in memory.active_commitments),
            "satisfied": completed,
            "blocked": (),
        },
        effects=tuple({
            "ref": event.event_ref,
            "statement": event.statement,
        } for event in memory.accumulated_evidence[-16:]),
        coverage={},
        latest_transition_ref=latest_transition_ref,
        audit_refs=audit_refs,
    )


def _transition_contract(
    feedback: dict[str, Any] | None,
) -> TransitionContract | None:
    if not feedback:
        return None
    status = str(feedback.get("status") or "")
    if status == "collection_traversal_boundary":
        directions = tuple(
            str(item) for item in feedback.get("boundary_directions") or () if item
        )
        return TransitionContract(
            mode="boundary_reconciliation",
            forbidden_directions=directions,
        )
    if status in {"rejected_before_dispatch", "memory_update_invalid"}:
        return TransitionContract(mode="same_frame_protocol_repair")
    return None


def build_current_state(
    *,
    frame: MaterializedFrame,
    observation: dict[str, Any],
    memory: WorkerMemoryView,
    spec: WorkerSpec | None,
    completion_mode: Literal["unavailable", "operator", "collector"],
    same_frame_feedback: dict[str, Any] | None,
) -> CurrentStateSnapshot:
    transaction = memory.target_transaction
    feedback = dict(same_frame_feedback or {})
    traversal = (
        {
            "mode": "boundary_reconciliation",
            "boundary_directions": feedback.get("boundary_directions") or [],
            "action_effect": feedback.get("action_effect"),
            "scope": "current_scroll_container_only",
        }
        if feedback.get("status") == "collection_traversal_boundary"
        else {}
    )
    completion_phase = (
        "not_ready" if completion_mode == "unavailable"
        else "reconciling" if traversal
        else "worker_decision_required"
    )
    input_requests = tuple(
        event.receipt for event in memory.recent_receipts
        if event.receipt is not None and event.receipt.tool == "ask_user"
    )
    requested_questions = tuple(dict.fromkeys(
        str(receipt.args.get("question") or "").strip()
        for receipt in input_requests
        if str(receipt.args.get("question") or "").strip()
    ))
    input_status = (
        "answered" if input_requests and input_requests[-1].executed else "failed"
    )
    user_input = (
        {
            "status": input_status,
            "requested_questions": requested_questions,
            "request_count_in_recent_window": len(input_requests),
            "instruction": (
                "Consume the recorded authoritative response. Do not ask any listed "
                "question again; a refusal or unavailable value is still a resolved "
                "request and requires another UI action or report_blocked."
            ),
        }
        if requested_questions else {}
    )
    return CurrentStateSnapshot(
        surface={
            "identity": frame.title or frame.url or "untitled_surface",
            "continuity": (
                "returned_to_anchor"
                if transaction is not None and transaction.status == "returned_to_anchor"
                else "current"
            ),
        },
        observation=observation,
        interaction=(
            {
                "target_ref": transaction.target.ref,
                "phase": transaction.status,
                "verified_effects": [event.statement for event in transaction.evidence],
            }
            if transaction is not None else {}
        ),
        traversal=traversal,
        recovery=(feedback or None),
        user_input=user_input,
        transition_contract=_transition_contract(same_frame_feedback),
        completion={
            "phase": completion_phase,
            "blocking_requirements": list(frame.missing_requirements),
            "target_resolution": "runtime_unknown",
            **(
                {
                    "required_source": spec.strategy.approach,
                    "source_alignment": "verify_from_current_frame",
                }
                if spec is not None and traversal else {}
            ),
        },
    )
