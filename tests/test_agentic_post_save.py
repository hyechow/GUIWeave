"""Push-forward regression: size-like fill → save → list page must not re-open editor.

Locks the Agentic path: Journal effect + commit receipts let one Transition completion proposal
finish without reopening the editor.
"""

from __future__ import annotations

from gui_agent.core.run.execution_signals import (
    CompletionReducer,
    ExecutionContract,
)
from gui_agent.core.run.persistence import assess_persistence
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.run.statement_transition import guard_complete
from gui_agent.core.schemas import (
    ActionSignal,
    BaseAction,
    BaseActionDecision,
    EffectSignal,
    MutationReceipt,
    Observation,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
    TargetVerify,
)
from gui_agent.core.supervisor.statement.evidence import action_lifecycle_claims
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionEvidence,
)


def _contract() -> StatementContract:
    return StatementContract(
        id="s_size_opts",
        name="在 Options 添加 30/31 并保存",
        description="Admin Description/Swatch 30 与 31 后 Save Attribute",
        success_condition="选项已保存",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
        target_values={
            "Admin Description": ["30", "31"],
            "Admin Swatch": ["30", "31"],
        },
    )


def _turn(
    index: int,
    *,
    role: str,
    instruction: str,
    signal: ActionSignal,
    effect: EffectSignal | None = None,
    instance_id: str = "i_size",
    statement_id: str = "s_size_opts",
    scope: str = "i_size/row:attr/144",
) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="browser",
        statement_instance_id=instance_id,
        supervisor=SupervisorStep(
            should_act=True,
            instruction=instruction,
            summary=instruction,
            statement_id=statement_id,
            atomic_role=role,
            execution_scope=scope,
        ),
        executed=True,
        action_decision=BaseActionDecision(
            action=BaseAction(
                action_type="tap" if role == "commit" else "type",
                description=instruction,
            )
        ),
        action_signal=signal,
        effect_signal=effect,
        target_verify=TargetVerify(on_target=True, actual_element="ok"),
    )


def _write(field: str, value: str) -> ActionSignal:
    return ActionSignal(
        role="write",
        action_key=f"write:{field}:{value}",
        target_control=field,
        target_value=value,
        execution="dispatched",
        target="on_target",
        response="observed",
        response_channels=["dom"],
        mutation_receipt=MutationReceipt(
            statement_id="s_size_opts",
            subject_ref="collection:options",
            field=field,
            intended_value=value,
            source="structural",
        ),
    )


def _commit() -> ActionSignal:
    return ActionSignal(
        role="commit",
        action_key="commit:Save Attribute",
        target_control="Save Attribute",
        execution="dispatched",
        target="on_target",
        response="observed",
        response_channels=["url", "dom"],
        mutation_receipt=MutationReceipt(
            statement_id="s_size_opts",
            subject_ref="collection:options",
            field="",
            intended_value="",
            source="structural",
        ),
    )


def _size_history() -> list[PolicyTurn]:
    effect = EffectSignal(
        statement_id="s_size_opts",
        status="satisfied",
        source_type="obs.mutation.desired_state",
        authoritative=True,
        subject_ref="collection:options",
        evidence=["Admin Description/Swatch 30 and 31 present in Options grid"],
    )
    return [
        _turn(10, role="write", instruction="fill Admin Description 30", signal=_write("Admin Description", "30")),
        _turn(11, role="write", instruction="fill Admin Swatch 30", signal=_write("Admin Swatch", "30")),
        _turn(12, role="write", instruction="fill Admin Description 31", signal=_write("Admin Description", "31")),
        _turn(
            13,
            role="write",
            instruction="fill Admin Swatch 31",
            signal=_write("Admin Swatch", "31"),
            effect=effect,
        ),
        _turn(14, role="commit", instruction="click Save Attribute", signal=_commit()),
    ]


def test_memory_after_save_preserves_receipts_without_route_instructions() -> None:
    view = build_memory_view(
        instance_id="i_size",
        contract=_contract(),
        history=_size_history(),
        recent_k=2,
    )
    kinds = {f.kind for f in view.durable_facts}
    assert "effect_satisfied" in kinds
    assert "mutation_receipt" in kinds
    text = view.render_prompt_section()
    assert "effect=satisfied" in text or "effect_satisfied" in text or "satisfied" in text
    assert "重新点进" not in text
    assert "优先考虑" not in text


def test_evidence_and_guard_complete_on_list_page_without_options_dom() -> None:
    """List page after Save has no Options controls — journal still completes."""
    statement = _contract()
    history = _size_history()
    scope = "i_size/row:attr/144"
    claims = action_lifecycle_claims(statement, history, scope=scope)
    # Live observation is the attributes list (no option rows).
    claims.extend([])  # no live target_value claims
    evaluation = CompletionReducer().decide(
        ExecutionContract.from_statement(statement),
        claims,
        scope=scope,
        persistence_assessment=assess_persistence(statement, history, scope=scope),
    )
    assert evaluation.status == "satisfied"
    assert evaluation.completion_status in {"confirmed", "accepted_unverified"}

    assert guard_complete(evaluation).allowed


def test_policy_completes_post_save_from_memory_without_reopen(monkeypatch) -> None:
    statement = _contract()
    history = _size_history()
    policy = StatementSupervisorPolicy()
    policy.begin_statement(statement, instance_id="i_size")
    monkeypatch.setattr(
        policy,
        "_invoke_statement_transition",
        lambda *a, **k: _StatementTransitionResult(
            kind="complete",
            reason="Journal records the option writes and Save dispatch",
            evidence=[
                _TransitionEvidence(
                    source="journal",
                    event_ref="turn:14",
                    claim="Save Attribute was dispatched after the option writes",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        "gui_agent.core.supervisor.statement.policy.is_loading_frame",
        lambda _obs: False,
    )

    step = policy._run_single_turn(
        statement,
        Observation(
            png_bytes=b"\x89PNG\r\n\x1a\n",
            source="browser",
            url="http://x/admin/catalog/product_attribute/",
            form_controls=[
                {
                    "kind": "text_input",
                    "label": "Attribute Code",
                    "value": "size",
                    "in_viewport": True,
                }
            ],
            # No Options / Admin Description on list page — classic post-save surface.
        ),
        history,
    )

    assert step.outcome is not None
    assert step.outcome.phase == "completed"
    assert step.should_act is False
    assert step.outcome.verification in {"confirmed", "accepted_unverified"}
