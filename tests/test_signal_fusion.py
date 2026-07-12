from gui_agent.core.run.execution_signals import (
    CompletionEvaluator,
    ConstraintLedger,
    ExecutionContract,
    claim,
)
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Milestone,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone.action_protocol import (
    action_metadata,
    is_commit_turn,
)
from gui_agent.core.supervisor.milestone.schemas import _PlanResult


def test_action_metadata_preserves_structured_commit_role_and_family():
    plan = _PlanResult(
        instruction="按回车提交当前字段",
        summary="submit",
        atomic_role="commit",
        action_family="input",
    )

    role, family = action_metadata(
        plan,
        Milestone(
            id="filter",
            name="submit filter",
            description="",
            success_condition="filter is applied",
            kind="filter",
        ),
    )

    assert role == "commit"
    assert family == "input"


def test_action_metadata_preserves_structured_write_role_and_family():
    plan = _PlanResult(
        instruction="在目标字段输入 value",
        summary="write",
        atomic_role="write",
        action_family="commit",
    )

    role, family = action_metadata(
        plan,
        Milestone(
            id="write",
            name="write value",
            description="",
            success_condition="value is written",
            kind="action",
        ),
    )

    assert role == "write"
    assert family == "commit"


def test_commit_detection_uses_structured_role_not_instruction_vocabulary():
    milestone = Milestone(
        id="m",
        name="perform mutation",
        description="",
        success_condition="state is durable",
        kind="action",
    )
    turn = PolicyTurn(
        index=1,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True,
            instruction="trigger boundary",
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m",
            atomic_role="commit",
        ),
        action_decision=BaseActionDecision(
            action=BaseAction(action_type="tap", x=1, y=1)
        ),
        executed=True,
    )

    assert is_commit_turn(turn, milestone) is True
    assert is_commit_turn(
        turn.model_copy(update={
            "supervisor": turn.supervisor.model_copy(update={"atomic_role": "prepare"})
        }),
        milestone,
    ) is False


def test_filter_with_result_completes_when_applied_even_if_result_is_zero():
    contract = ExecutionContract(
        statement_id="filter",
        kind="filter",
        output_fields=("match_count",),
        completion_mode="filter_state_with_result",
    )
    decision = CompletionEvaluator().decide(
        contract,
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "filter.state",
                "confirmed",
                source_type="obs.applied_filters",
                scope="milestone:filter",
                authoritative=True,
            ),
            claim(
                "result.availability",
                "confirmed",
                source_type="obs.tables",
                scope="milestone:filter",
                evidence="match_count=0",
                authoritative=True,
            ),
        ],
        scope="milestone:filter",
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "confirmed"


def test_commit_dispatch_without_outcome_feedback_is_provisional():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        require_fresh_action=True,
        require_terminal_dispatch=True,
        completion_mode="mutation",
    )
    decision = CompletionEvaluator().decide(
        contract,
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.commit_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "unverified",
                source_type="checker",
                scope="row:7",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "accepted_unverified"


def test_commit_cannot_be_accepted_while_declared_target_values_are_incomplete():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        require_fresh_action=True,
        require_terminal_dispatch=True,
        completion_mode="mutation",
    )
    decision = CompletionEvaluator().decide(
        contract,
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.commit_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "control.state",
                "contradicted",
                source_type="obs.dom.target_values",
                scope="row:7",
                evidence="declared fields are incomplete",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "unverified",
                source_type="checker",
                scope="row:7",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "pending"
    assert decision.completion_status == "in_progress"
    assert decision.conflicts == ("target.values.incomplete",)


def test_confirmed_outcome_cannot_replace_required_terminal_dispatch():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        require_fresh_action=True,
        require_terminal_dispatch=True,
        completion_mode="mutation",
    )
    decision = CompletionEvaluator().decide(
        contract,
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.action_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "confirmed",
                source_type="checker",
                scope="row:7",
                evidence="possibly stale success cue",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "pending"
    assert "终端提交尚未派发" in decision.reason


def test_change_mutation_cannot_complete_from_commit_without_write():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="save",
            kind="action",
            require_fresh_action=True,
            require_terminal_dispatch=True,
            completion_mode="mutation",
            mutation_mode="change",
        ),
        [
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.commit_dispatch",
                scope="milestone:save",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "unverified",
                source_type="checker",
                scope="milestone:save",
            ),
        ],
        scope="milestone:save",
    )

    assert decision.status == "pending"
    assert decision.completion_status == "in_progress"


def test_ensure_mutation_accepts_authoritative_preexisting_outcome():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="ensure",
            kind="action",
            completion_mode="mutation",
            mutation_mode="ensure",
        ),
        [
            claim(
                "business.outcome",
                "confirmed",
                source_type="checker",
                scope="milestone:ensure",
                evidence="target state already exists",
            )
        ],
        scope="milestone:ensure",
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "confirmed"


def test_ensure_mutation_with_fresh_draft_write_requires_declared_commit():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="ensure_saved",
            kind="action",
            require_terminal_dispatch=True,
            completion_mode="mutation",
            mutation_mode="ensure",
        ),
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope="row:attribute/144",
                authoritative=True,
            ),
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.action_dispatch",
                scope="row:attribute/144",
                authoritative=True,
            ),
            claim(
                "control.state",
                "confirmed",
                source_type="obs.dom.target_values",
                scope="row:attribute/144",
                evidence="all declared fields match in one structural unit",
                authoritative=True,
            ),
        ],
        scope="row:attribute/144",
    )

    assert decision.status == "pending"
    assert decision.conflicts == ("action.commit.required",)
    assert "草稿状态" in decision.reason


def test_ensure_mutation_with_preexisting_control_state_still_skips_commit():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="ensure_existing",
            kind="action",
            require_terminal_dispatch=True,
            completion_mode="mutation",
            mutation_mode="ensure",
        ),
        [
            claim(
                "control.state",
                "confirmed",
                source_type="obs.dom.target_values",
                scope="row:attribute/144",
                evidence="target member already persisted before this run",
                authoritative=True,
            ),
        ],
        scope="row:attribute/144",
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "confirmed"


def test_commit_without_target_write_returns_typed_recovery_conflict():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="ensure",
            kind="action",
            require_terminal_dispatch=True,
            completion_mode="mutation",
            mutation_mode="ensure",
        ),
        [claim(
            "action.execution",
            "confirmed",
            source_type="runtime.commit_dispatch",
            scope="milestone:ensure",
            authoritative=True,
        )],
        scope="milestone:ensure",
    )

    assert decision.status == "pending"
    assert decision.conflicts == ("action.write.required",)


def test_generic_page_response_cannot_complete_navigation():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="open",
            kind="navigation",
            completion_mode="arrival",
        ),
        [
            claim(
                "page.response",
                "confirmed",
                source_type="runtime.effect_monitor",
                scope="milestone:open",
                authoritative=True,
            )
        ],
        scope="milestone:open",
    )

    assert decision.status == "pending"


def test_partial_inventory_absence_is_not_a_negative_control_claim():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="edit",
            kind="action",
            completion_mode="mutation",
        ),
        [
            claim(
                "inventory.coverage",
                "partial",
                source_type="obs.dom",
                scope="milestone:edit",
                authoritative=True,
                coverage="partial",
            )
        ],
        scope="milestone:edit",
    )

    assert decision.status == "pending"


def test_runtime_constraint_is_visible_only_in_its_scope():
    ledger = ConstraintLedger()
    ledger.add("do not repeat this scroll", scope="milestone:first")

    assert ledger.visible("milestone:first") == ["do not repeat this scroll"]
    assert ledger.visible("milestone:second") == []


def test_delegation_is_an_explicit_evidence_status_not_completion():
    contract = ExecutionContract(
        statement_id="filter",
        kind="filter",
        completion_mode="filter_state",
    )
    decision = CompletionEvaluator().decide(
        contract,
        [claim(
            "execution.delegation",
            "confirmed",
            source_type="runtime.filter_fallback",
            scope="milestone:filter",
            authoritative=True,
        )],
        scope="milestone:filter",
    )

    assert decision.status == "delegated"
    assert decision.completion_status == "in_progress"
