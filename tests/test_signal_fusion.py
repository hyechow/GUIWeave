from gui_agent.core.run.execution_signals import (
    ActionConstraint,
    ConstraintLedger,
    ExecutionContract,
    SignalFusionArbiter,
    claim,
    validate_action_family,
)


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
