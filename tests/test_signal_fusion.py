from gui_agent.core.run.execution_signals import (
    ActionConstraint,
    ConstraintLedger,
    ExecutionContract,
    SignalFusionArbiter,
    claim,
    validate_action_family,
)
from gui_agent.core.schemas import Milestone
from gui_agent.core.supervisor.milestone.policy import _resolved_plan_action_family
from gui_agent.core.supervisor.milestone.schemas import _PlanResult


def test_commit_role_normalizes_input_family_before_primitive_validation():
    plan = _PlanResult(
        instruction="按回车提交当前字段",
        summary="submit",
        atomic_role="commit",
        action_family="input",
    )

    role, family = _resolved_plan_action_family(
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
    assert family == "commit"
    assert validate_action_family(family, "press_enter") == (True, "")


def test_write_role_does_not_widen_input_family_to_terminal_primitives():
    plan = _PlanResult(
        instruction="在目标字段输入 value",
        summary="write",
        atomic_role="write",
        action_family="commit",
    )

    role, family = _resolved_plan_action_family(
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
    assert family == "input"
    allowed, _reason = validate_action_family(family, "press_enter")
    assert allowed is False


def test_filter_with_result_completes_when_applied_even_if_result_is_zero():
    contract = ExecutionContract(
        statement_id="filter",
        kind="filter",
        output_fields=("match_count",),
        completion_mode="filter_state_with_result",
    )
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "complete"
    assert decision.completion_status == "confirmed"


def test_commit_dispatch_without_outcome_feedback_is_provisional():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        require_fresh_action=True,
        require_terminal_dispatch=True,
        completion_mode="mutation",
    )
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "complete"
    assert decision.completion_status == "accepted_unverified"


def test_confirmed_outcome_cannot_replace_required_terminal_dispatch():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        require_fresh_action=True,
        require_terminal_dispatch=True,
        completion_mode="mutation",
    )
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "continue"
    assert "终端提交尚未派发" in decision.reason


def test_change_mutation_cannot_complete_from_commit_without_write():
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "continue"
    assert decision.completion_status == "in_progress"


def test_ensure_mutation_accepts_authoritative_preexisting_outcome():
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "complete"
    assert decision.completion_status == "confirmed"


def test_commit_without_target_write_returns_typed_recovery_conflict():
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "continue"
    assert decision.conflicts == ("action.write.required",)


def test_named_target_rejects_adjacent_input_control():
    decision = SignalFusionArbiter().validate_proposal(
        "input",
        [
            ActionConstraint(
                scope="milestone:filter",
                source_type="contract.target_controls",
                evidence="named field",
                required_targets=("Name",),
            )
        ],
        scope="milestone:filter",
        target_control="Search by keyword",
    )

    assert decision.action == "reject_action"
    assert "相邻控件" in decision.reason


def test_generic_page_response_cannot_complete_navigation():
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "continue"


def test_partial_inventory_absence_is_not_a_negative_control_claim():
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "continue"


def test_action_family_rejects_tap_for_input_before_dispatch():
    allowed, reason = validate_action_family("input", "tap")
    assert allowed is False
    assert "input" in reason


def test_runtime_constraint_is_visible_only_in_its_scope():
    ledger = ConstraintLedger()
    ledger.add("do not repeat this scroll", scope="milestone:first")

    assert ledger.visible("milestone:first") == ["do not repeat this scroll"]
    assert ledger.visible("milestone:second") == []


def test_delegation_is_an_explicit_arbiter_decision_not_completion():
    contract = ExecutionContract(
        statement_id="filter",
        kind="filter",
        completion_mode="filter_state",
    )
    decision = SignalFusionArbiter().decide(
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

    assert decision.action == "delegate"
    assert decision.completion_status == "in_progress"


def test_proposal_arbiter_blocks_commit_until_required_row_is_complete():
    decision = SignalFusionArbiter().validate_proposal(
        "commit",
        [ActionConstraint(
            scope="milestone:add-option",
            source_type="obs.dom.form_validity",
            evidence="target row still has a required empty field",
            blocked_families=("commit",),
        )],
        scope="milestone:add-option",
    )

    assert decision.action == "reject_action"
    assert "required empty field" in decision.reason


def test_proposal_arbiter_allows_required_input_family():
    decision = SignalFusionArbiter().validate_proposal(
        "input",
        [ActionConstraint(
            scope="milestone:search",
            source_type="obs.dom.control_ready",
            evidence="target input is visible and empty",
            allowed_families=("input",),
        )],
        scope="milestone:search",
    )

    assert decision.action == "allow_action"


def test_proposal_constraints_do_not_cross_execution_scopes():
    decision = SignalFusionArbiter().validate_proposal(
        "commit",
        [ActionConstraint(
            scope="milestone:first",
            source_type="obs.dom.form_validity",
            evidence="first form is incomplete",
            blocked_families=("commit",),
        )],
        scope="milestone:second",
    )

    assert decision.action == "allow_action"
