from gui_agent.core.run.statement_memory import (
    available_event_refs,
    build_memory_view,
    durable_kinds_present,
)
from gui_agent.core.schemas import (
    ActionIntent,
    ActionSignal,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
)


def _contract() -> StatementContract:
    return StatementContract(
        id="edit",
        goal="add requested values to the current entity",
        success="all requested values are present and saved",
        inputs={"entity": {"id": "A"}},
        required_values={"values": ["30", "31"]},
        persistence="explicit_commit",
    )


def _turn(
    index: int,
    instance_id: str,
    *,
    dispatched: bool = True,
) -> PolicyTurn:
    intent = ActionIntent(
        instruction=f"perform step {index}",
        role="write",
        family="input",
        target_control="value field",
        target_value=str(index),
    )
    return PolicyTurn(
        index=index,
        observation_source="browser",
        statement_instance_id=instance_id,
        supervisor=SupervisorStep(
            statement_id="edit",
            summary=f"step {index}",
            action_intent=intent,
        ),
        executed=dispatched,
        action_signal=ActionSignal(
            role="write",
            execution="dispatched" if dispatched else "dispatch_failed",
            response="observed" if dispatched else "unknown",
        ),
    )


def test_memory_is_a_read_only_journal_projection_with_invocation_inputs():
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_turn(1, "i1:edit"), _turn(2, "other")],
    )

    rendered = view.render_prompt_section()
    assert '"id": "A"' in rendered
    assert "30,31" in rendered
    assert [step.event_ref for step in view.recent_steps] == ["turn:1"]
    assert not hasattr(view, "phase")


def test_action_receipts_survive_narrative_compaction():
    history = [_turn(index, "i1:edit") for index in range(1, 10)]
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=history,
        recent_k=2,
        compressed_k=2,
    )

    assert len(view.recent_steps) == 2
    assert len(view.compressed_history) == 2
    assert len(view.durable_facts) == 9
    assert durable_kinds_present(view) == {"action_receipt"}
    assert available_event_refs(view) == {f"turn:{index}" for index in range(1, 10)}


def test_failed_dispatch_is_retained_as_a_fact_not_a_runtime_phase():
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_turn(1, "i1:edit", dispatched=False)],
    )
    assert durable_kinds_present(view) == {"dispatch_failure"}


def test_recent_history_excludes_prior_model_assessments():
    base = _turn(1, "i1:edit")
    turn = base.model_copy(update={
        "supervisor": base.supervisor.model_copy(
            update={"summary": "unsupported model belief"},
        ),
        "transition": {
            "proposal": {
                "assessment": {"status": "in_progress"},
                "kind": "act",
            },
            "validation_error": "",
        },
    })
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[turn],
    )

    text = view.recent_steps[0].text
    assert "unsupported model belief" not in text
    assert "模型状态" not in text
    assert "模型决定" not in text
    assert "指令[write]" in text
    assert "signal exec=dispatched" in text
