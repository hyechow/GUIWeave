"""StatementMemoryView: journal projection, durable facts, compaction."""

from __future__ import annotations

from gui_agent.core.schemas import ActionIntent

from gui_agent.core.run.statement_memory import (
    available_event_refs,
    build_memory_view,
    durable_kinds_present,
)
from gui_agent.core.schemas import (
    ActionSignal,
    EffectSignal,
    MutationReceipt,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
)


def _contract(**updates) -> StatementContract:
    base = dict(
        id="s_opts",
        name="add options and save",
        description="fill option rows then save",
        success_condition="options saved",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
        target_values={"Admin Description": ["30", "31"]},
        returns=[],
    )
    base.update(updates)
    return StatementContract(**base)


def _turn(
    index: int,
    *,
    instance_id: str = "i1",
    statement_id: str = "s_opts",
    role: str = "write",
    instruction: str = "fill field",
    executed: bool = True,
    signal: ActionSignal | None = None,
    effect: EffectSignal | None = None,
    summary: str = "",
) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="test",
        statement_instance_id=instance_id,
        supervisor=SupervisorStep(action_intent=ActionIntent(instruction=instruction, role=role), summary=summary or instruction, statement_id=statement_id),
        executed=executed,
        action_signal=signal,
        effect_signal=effect,
    )


def _write_signal(field: str, value: str) -> ActionSignal:
    return ActionSignal(
        role="write",
        action_key=f"write:{field}",
        target_control=field,
        target_value=value,
        execution="dispatched",
        target="on_target",
        response="observed",
        mutation_receipt=MutationReceipt(
            statement_id="s_opts",
            subject_ref="row:options",
            field=field,
            intended_value=value,
            source="structural",
        ),
    )


def _commit_signal() -> ActionSignal:
    return ActionSignal(
        role="commit",
        action_key="commit:Save",
        target_control="Save Attribute",
        execution="dispatched",
        target="on_target",
        response="observed",
        response_channels=["url"],
        mutation_receipt=MutationReceipt(
            statement_id="s_opts",
            subject_ref="row:options",
            field="",
            intended_value="",
            source="structural",
        ),
    )


def test_memory_view_has_no_phase_attribute() -> None:
    view = build_memory_view(
        instance_id="i1",
        contract=_contract(),
        history=[],
    )
    assert not hasattr(view, "phase")
    assert not hasattr(view, "subphase")
    assert "phase" not in view.__dataclass_fields__


def test_build_memory_keeps_write_commit_effect_across_window() -> None:
    """size-like path: many filler turns must not compact away durable receipts/effects."""
    history = [
        _turn(1, role="prepare", instruction="scroll options", signal=ActionSignal(
            role="iterate",
            action_key="scroll",
            execution="dispatched",
            target="unknown",
            response="observed",
        )),
        _turn(
            2,
            role="write",
            instruction="set Admin Description 30",
            signal=_write_signal("Admin Description", "30"),
        ),
        _turn(
            3,
            role="write",
            instruction="set Admin Swatch 30",
            signal=_write_signal("Admin Swatch", "30"),
        ),
        _turn(
            4,
            role="write",
            instruction="set Admin Description 31",
            signal=_write_signal("Admin Description", "31"),
        ),
        _turn(
            5,
            role="write",
            instruction="set Admin Swatch 31",
            signal=_write_signal("Admin Swatch", "31"),
            effect=EffectSignal(
                statement_id="s_opts",
                status="satisfied",
                source_type="obs.mutation.desired_state",
                authoritative=True,
                evidence=["rows 30 and 31 visible"],
            ),
        ),
        _turn(
            6,
            role="commit",
            instruction="click Save Attribute",
            signal=_commit_signal(),
        ),
        # Routine observes after navigation — push durable facts out of recent window.
        *[_turn(7 + i, role="prepare", instruction=f"look around {i}", executed=False)
          for i in range(8)],
    ]

    view = build_memory_view(
        instance_id="i1",
        contract=_contract(),
        history=history,
        recent_k=3,
    )

    kinds = durable_kinds_present(view)
    assert "mutation_receipt" in kinds
    assert "effect_satisfied" in kinds
    assert "action_receipt" in kinds

    # Compaction: recent window is short, older lines still reference durable turns.
    assert len(view.recent_steps) == 3
    assert any("turn:2" in line or "turn:5" in line or "turn:6" in line
               for line in view.compressed_history) or view.durable_facts

    # Open commitment should note transform+commit facts (commit seen; effect may close write gap).
    text = view.render_prompt_section()
    assert "StatementMemory" in text
    assert "explicit_commit" in text or "commit" in text.lower()
    assert any(f.kind == "effect_satisfied" for f in view.durable_facts)


def test_off_target_and_dispatch_failure_are_durable() -> None:
    history = [
        _turn(
            1,
            role="write",
            instruction="tap size row",
            signal=ActionSignal(
                role="prepare",
                action_key="tap",
                execution="dispatched",
                target="off_target",
                response="none_observed",
            ),
        ),
        _turn(
            2,
            role="commit",
            instruction="save",
            signal=ActionSignal(
                role="commit",
                action_key="commit",
                execution="dispatch_failed",
                target="unknown",
                response="unknown",
            ),
        ),
    ]
    view = build_memory_view(instance_id="i1", contract=_contract(), history=history, recent_k=1)
    kinds = durable_kinds_present(view)
    assert "off_target" in kinds
    assert "dispatch_failure" in kinds
    # recent_k=1 would drop turn 1 from recent_steps but durable facts remain
    assert any(f.event_ref == "turn:1" for f in view.durable_facts)


def test_instance_isolation() -> None:
    history = [
        _turn(1, instance_id="i1", signal=_write_signal("A", "1")),
        _turn(2, instance_id="i2", signal=_write_signal("B", "2")),
    ]
    view = build_memory_view(instance_id="i1", contract=_contract(), history=history)
    texts = " ".join(f.text for f in view.durable_facts)
    assert "A" in texts
    assert "B" not in texts


def test_ownership_memory_module_has_no_phase_and_is_projection_only() -> None:
    from gui_agent.core.run.statement_memory import StatementMemoryView, build_memory_view

    assert callable(build_memory_view)
    fields = set(StatementMemoryView.__dataclass_fields__)
    # Must not introduce a business phase FSM on the view.
    assert "phase" not in fields
    assert "subphase" not in fields
    assert fields >= {
        "instance_id",
        "durable_facts",
        "recent_steps",
        "compressed_history",
        "contract_requirements",
    }


def test_narrative_compaction_is_bounded() -> None:
    history = [
        _turn(i, role="prepare", instruction=f"routine step {i}", executed=False)
        for i in range(1, 41)
    ]
    view = build_memory_view(
        instance_id="i1",
        contract=_contract(),
        history=history,
        recent_k=4,
        compressed_k=5,
    )
    assert len(view.recent_steps) == 4
    assert len(view.compressed_history) == 5


def test_old_non_mutation_actions_remain_available_as_receipts() -> None:
    history = [
        _turn(
            index,
            role="prepare",
            instruction=f"打开第 {index} 个入口",
            signal=ActionSignal(
                role="prepare",
                action_key=f"open:{index}",
                execution="dispatched",
                target="on_target",
                response="observed",
            ),
        )
        for index in range(1, 20)
    ]

    view = build_memory_view(
        instance_id="i1",
        contract=_contract(),
        history=history,
        recent_k=2,
        compressed_k=2,
    )

    receipts = [fact for fact in view.durable_facts if fact.kind == "action_receipt"]
    assert len(receipts) == 19
    assert receipts[0].event_ref == "turn:1"
    assert "打开第 1 个入口" in receipts[0].text
    assert "turn:1" in available_event_refs(view)


def test_memory_contains_facts_not_route_instructions() -> None:
    view = build_memory_view(
        instance_id="i1",
        contract=_contract(),
        history=[
            _turn(1, signal=_write_signal("Admin Description", "30")),
            _turn(2, role="commit", signal=_commit_signal()),
        ],
    )
    text = view.render_prompt_section()
    assert "不得把" not in text
    assert "优先考虑" not in text
    assert "重新点进" not in text
