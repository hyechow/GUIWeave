from __future__ import annotations

from gui_agent.core.tool_agent.worker_memory import WorkerJournal


def _verified_tap(
    journal: WorkerJournal,
    *,
    step: int,
    surface: str = "anchor",
    x: int = 100,
    y: int = 200,
) -> None:
    journal.record_action_result(
        step=step,
        frame_id=f"frame:{step}",
        surface_fingerprint=surface,
        tool="tap",
        args={"x": x, "y": y, "description": "Activate the requested control"},
        result={
            "status": "executed",
            "action_type": "tap",
            "no_effect": False,
            "target_signal": {
                "status": "on_target",
                "actual_element": "Requested control",
                "container_context": "Record alpha",
            },
        },
    )


def test_runtime_commitment_lifecycle_exposes_only_active_refs() -> None:
    journal = WorkerJournal(worker_id="runtime_commitment")
    dispatched = journal.record_runtime_commitment(
        step=1,
        substep=1,
        frame_id="frame:1",
        tool="tap",
        statement="Activate the verified control",
    )

    assert journal.active_commitment_refs == ("commitment:action_1_1",)

    settled = journal.settle_runtime_commitment(
        step=1,
        substep=1,
        frame_id="frame:1",
        tool="tap",
        statement="Activate the verified control",
        result={"status": "executed", "action_type": "tap", "no_effect": False},
    )

    assert dispatched.origin == settled.origin == "runtime"
    assert settled.status == "satisfied"
    assert journal.active_commitment_refs == ()


def test_active_fact_statements_follow_runtime_fact_versions() -> None:
    journal = WorkerJournal(worker_id="facts")
    journal.record_runtime_input(key="code", statement="Code is 123456")
    journal.record_runtime_input(key="code", statement="Code is 654321")
    journal.record_runtime_commitment(
        step=1,
        substep=1,
        frame_id="frame:1",
        tool="type",
        statement="Enter code 654321",
    )

    assert journal.active_fact_statements(frame_id="frame:2") == (
        "Code is 654321",
        "Enter code 654321",
    )


def test_verified_action_records_runtime_evidence_and_target() -> None:
    journal = WorkerJournal(worker_id="receipt_order")
    _verified_tap(journal, step=1)

    evidence = next(event for event in journal.events if event.fact_ref)
    assert evidence.fact_ref == "evidence:verified_action_1"
    assert evidence.origin == "runtime"
    assert "actual_target='Requested control'" in evidence.statement
    assert journal.active_target is not None
    assert journal.active_target.container_context == "Record alpha"
    assert evidence.target_ref == journal.active_target.ref


def test_verified_target_on_new_surface_starts_new_target() -> None:
    journal = WorkerJournal(worker_id="new_surface_target")
    _verified_tap(journal, step=1, surface="first")
    first_ref = journal.active_target.ref  # type: ignore[union-attr]

    _verified_tap(journal, step=2, surface="second", x=900, y=300)

    assert journal.active_target is not None
    assert journal.active_target.ref != first_ref
    assert journal.active_target.anchor_surface_fingerprint == "second"
    assert journal.latest_action_receipt is not None
    assert journal.latest_action_receipt.target_ref == journal.active_target.ref


def test_effectful_traversal_clears_target_but_no_effect_preserves_it() -> None:
    journal = WorkerJournal(worker_id="target_traversal")
    _verified_tap(journal, step=1)
    journal.record_action_result(
        step=2,
        frame_id="frame:2",
        surface_fingerprint="anchor",
        tool="scroll",
        args={"direction": "down"},
        result={"status": "executed", "action_type": "scroll", "no_effect": True},
    )
    assert journal.active_target is not None

    journal.record_action_result(
        step=3,
        frame_id="frame:3",
        surface_fingerprint="anchor",
        tool="scroll",
        args={"direction": "down"},
        result={"status": "executed", "action_type": "scroll", "no_effect": False},
    )
    assert journal.active_target is None


def test_executed_back_records_runtime_effect_without_target() -> None:
    journal = WorkerJournal(worker_id="back_receipt")
    journal.record_action_result(
        step=2,
        frame_id="frame:2",
        tool="back",
        args={"description": "Dismiss the open menu"},
        result={"status": "executed", "action_type": "back", "no_effect": False},
    )

    evidence = next(event for event in journal.events if event.fact_ref)
    assert evidence.fact_ref == "evidence:verified_action_2"
    assert "intent='Dismiss the open menu'" in evidence.statement
    assert "tool='back'" in evidence.statement
    assert "actual_target" not in evidence.statement


def test_action_receipt_omits_spatial_coordinates_from_semantic_args() -> None:
    journal = WorkerJournal(worker_id="semantic_receipt")
    _verified_tap(journal, step=1, x=420, y=240)

    receipt = journal.latest_action_receipt
    assert receipt is not None
    assert receipt.args == {"description": "Activate the requested control"}
    assert receipt.target is not None
    assert receipt.target.point == (420.0, 240.0)
