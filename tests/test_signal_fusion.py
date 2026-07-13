from gui_agent.core.run.execution_signals import (
    CompletionEvaluator,
    ConstraintLedger,
    ExecutionContract,
    claim,
)
from gui_agent.core.schemas import (
    ActionSignal,
    BaseAction,
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone.action_protocol import (
    action_metadata,
    concrete_persistence_plan,
    is_commit_turn,
    parent_persistence_pending,
    regresses_preparation_frontier,
)
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def _signal_turn(
    index: int,
    *,
    role: str,
    control: str,
    surface: str = "",
) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True,
            instruction=control,
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="persisted",
            atomic_role=role,
            target_control=control,
        ),
        executed=True,
        action_signal=ActionSignal(
            role=role,
            surface_id=surface,
            target_control=control,
            execution="dispatched",
            target="on_target",
            response="observed",
        ),
    )


def _redirected_mutation_contract() -> ExecutionContract:
    return ExecutionContract(
        statement_id="save",
        kind="action",
        require_terminal_dispatch=True,
        completion_mode="mutation",
    )


def test_destination_page_state_cannot_contradict_source_resource_mutation() -> None:
    scope = "milestone:save"
    decision = CompletionEvaluator().decide(
        _redirected_mutation_contract(),
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope=scope,
                subject_scope="row:record/65",
                authoritative=True,
            ),
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.commit_dispatch",
                scope=scope,
                subject_scope="row:record/65",
                authoritative=True,
            ),
            claim(
                "control.state",
                "contradicted",
                source_type="obs.dom.target_values",
                scope=scope,
                subject_scope="milestone:save",
                evidence="destination list has no source form controls",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "contradicted",
                source_type="checker",
                scope=scope,
                subject_scope="milestone:save",
                evidence="source form is no longer visible",
            ),
        ],
        scope=scope,
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "accepted_unverified"


def test_authoritative_operation_failure_for_source_resource_wins_after_redirect() -> None:
    scope = "milestone:save"
    decision = CompletionEvaluator().decide(
        _redirected_mutation_contract(),
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope=scope,
                subject_scope="row:record/65",
                authoritative=True,
            ),
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.commit_dispatch",
                scope=scope,
                subject_scope="row:record/65",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "contradicted",
                source_type="adapter.persistence_result",
                scope=scope,
                subject_scope="row:record/65",
                evidence="the operation was rejected",
                authoritative=True,
            ),
        ],
        scope=scope,
    )

    assert decision.status == "contradicted"


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


def test_persisted_mutation_downgrades_nested_activate_from_commit_role():
    plan = _PlanResult(
        instruction="generate nested draft rows",
        summary="return generated rows to the outer editor",
        atomic_role="commit",
        action_family="activate",
    )

    role, family = action_metadata(
        plan,
        Milestone(
            id="persisted",
            name="update collection and save",
            description="",
            success_condition="saved collection contains target rows",
            kind="action",
            requires_commit=True,
            target_values={"member": "target"},
        ),
    )

    assert role == "prepare"
    assert family == "activate"


def test_persisted_mutation_promotes_commit_family_to_terminal_role():
    plan = _PlanResult(
        instruction="save outer resource",
        summary="persist draft",
        atomic_role="prepare",
        action_family="commit",
    )

    role, family = action_metadata(
        plan,
        Milestone(
            id="persisted",
            name="update collection and save",
            description="",
            success_condition="saved collection contains target rows",
            kind="action",
            requires_commit=True,
        ),
    )

    assert role == "commit"
    assert family == "commit"


def test_persisted_mutation_rejects_backward_preparation_edge():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open editor"),
        _signal_turn(2, role="write", control="Target value"),
        _signal_turn(3, role="prepare", control="Generate draft"),
    ]
    proposal = _PlanResult(
        instruction="open the editor again",
        summary="",
        atomic_role="prepare",
        action_family="activate",
        target_control="Open editor",
    )

    assert regresses_preparation_frontier(proposal, milestone, history) is True
    assert regresses_preparation_frontier(
        proposal.model_copy(update={"target_control": "Save", "action_family": "commit"}),
        milestone,
        history,
    ) is False


def test_same_control_name_on_a_different_surface_is_not_a_backward_edge():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Next", surface="wizard:step-1"),
        _signal_turn(2, role="write", control="Target A", surface="wizard:step-2"),
    ]
    proposal = _PlanResult(
        instruction="advance",
        summary="",
        atomic_role="prepare",
        action_family="activate",
        target_control="Next",
    )

    assert regresses_preparation_frontier(
        proposal,
        milestone,
        history,
        current_surface_id="wizard:step-2",
    ) is False
    assert regresses_preparation_frontier(
        proposal,
        milestone,
        history,
        current_surface_id="wizard:step-1",
    ) is True


def test_parent_persistence_becomes_pending_when_child_entry_reappears_after_progress():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor"),
        _signal_turn(2, role="prepare", control="Next"),
        _signal_turn(3, role="write", control="Target A"),
        _signal_turn(4, role="write", control="Target B"),
        _signal_turn(5, role="prepare", control="Next"),
        _signal_turn(6, role="prepare", control="Generate draft"),
    ]
    assert parent_persistence_pending(
        milestone,
        history,
        {"Open child editor", "Persist resource"},
    ) is True


def test_repeated_in_flow_control_does_not_look_like_parent_return():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Next"),
        _signal_turn(2, role="write", control="Target A"),
        _signal_turn(3, role="prepare", control="Next"),
    ]

    assert parent_persistence_pending(milestone, history, {"next"}) is False


def test_writes_alone_do_not_make_parent_persistence_pending():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor"),
        _signal_turn(2, role="prepare", control="Advance child flow"),
        _signal_turn(3, role="write", control="Target A"),
        _signal_turn(4, role="write", control="Target B"),
    ]

    assert parent_persistence_pending(
        milestone,
        history,
        {"Open child editor", "Advance child flow"},
    ) is False


def test_visible_later_child_control_blocks_parent_return_even_when_entry_remains_in_dom():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor"),
        _signal_turn(2, role="write", control="Target A"),
        _signal_turn(3, role="prepare", control="Advance child flow"),
    ]

    assert parent_persistence_pending(
        milestone,
        history,
        {"openchildeditor", "advancechildflow"},
    ) is False


def test_policy_requires_concrete_commit_target_when_parent_entry_reappears():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor"),
        _signal_turn(2, role="write", control="Target A"),
        _signal_turn(3, role="prepare", control="Generate draft"),
    ]
    observation = Observation(
        png_bytes=b"frame",
        source="browser",
        semantic_tree=[
            {"role": "button", "key": "Open child editor", "ref": 17, "depth": 0},
        ],
    )
    policy = MilestoneSupervisorPolicy(
        active_target_resolver=lambda _observation: {
            "Open child editor",
            "Persist resource",
        }
    )
    policy._invoke_planner = lambda *_args, **_kwargs: _PlanResult(  # type: ignore[method-assign]
        instruction="persist the current resource",
        summary="cross the parent boundary",
        atomic_role="commit",
        action_family="commit",
        target_control="Persist resource",
    )

    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="state not yet durable",
            summary="returned to parent resource",
            outcome_status="unverified",
        ),
        observation,
        history,
    )

    assert step.atomic_role == "commit"
    assert step.action_family == "commit"
    assert step.target_control == "Persist resource"


def test_abstract_or_inactive_commit_target_is_not_concrete_persistence():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    abstract = _PlanResult(
        instruction="persist",
        summary="",
        atomic_role="commit",
        action_family="commit",
        target_control="",
    )
    wrong = abstract.model_copy(update={"target_control": "Inactive command"})
    concrete = abstract.model_copy(update={"target_control": "Persist resource"})

    assert concrete_persistence_plan(abstract, milestone, {"Persist resource"}) is False
    assert concrete_persistence_plan(wrong, milestone, {"Persist resource"}) is False
    assert concrete_persistence_plan(concrete, milestone, {"Persist resource"}) is True


def test_visual_regression_proposal_requires_a_new_forward_plan():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor"),
        _signal_turn(2, role="write", control="Target A"),
        _signal_turn(3, role="prepare", control="Generate draft"),
    ]
    proposals = iter(
        [
            _PlanResult(
                instruction="open the child editor again",
                summary="re-enter child flow",
                atomic_role="prepare",
                action_family="activate",
                target_control="Open child editor",
            ),
            _PlanResult(
                instruction="inspect a new forward surface",
                summary="do not revisit the old entry",
                atomic_role="iterate",
                action_family="iterate",
            ),
        ]
    )
    policy = MilestoneSupervisorPolicy()
    policy._invoke_planner = lambda *_args, **_kwargs: next(proposals)  # type: ignore[method-assign]

    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="state not yet durable",
            summary="visual-only parent surface",
            outcome_status="unverified",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        history,
    )

    assert step.atomic_role == "iterate"
    assert step.action_family == "iterate"


def test_visual_regression_that_persists_is_not_dispatched():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        requires_commit=True,
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor"),
        _signal_turn(2, role="write", control="Target A"),
        _signal_turn(3, role="prepare", control="Generate draft"),
    ]
    regression = _PlanResult(
        instruction="open the child editor again",
        summary="re-enter child flow",
        atomic_role="prepare",
        action_family="activate",
        target_control="Open child editor",
    )
    policy = MilestoneSupervisorPolicy()
    policy._invoke_planner = lambda *_args, **_kwargs: regression  # type: ignore[method-assign]

    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="state not yet durable",
            summary="visual-only parent surface",
            outcome_status="unverified",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        history,
    )

    assert step.should_act is False
    assert step.instruction is None


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


def test_authoritative_field_state_overrides_checker_pixel_contradiction():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="save",
            kind="action",
            require_fresh_action=True,
            require_terminal_dispatch=True,
            completion_mode="mutation",
        ),
        [
            claim(
                "action.write",
                "confirmed",
                source_type="runtime.write_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "control.state",
                "confirmed",
                source_type="obs.structured.target_values",
                scope="row:7",
                evidence="all declared fields match in one unit",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "contradicted",
                source_type="checker",
                scope="row:7",
                evidence="the screenshot appears to show placeholder text",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "pending"
    assert decision.conflicts == ("action.commit.required",)


def test_authoritative_business_failure_still_overrides_field_state():
    decision = CompletionEvaluator().decide(
        ExecutionContract(
            statement_id="save",
            kind="action",
            require_terminal_dispatch=True,
            completion_mode="mutation",
        ),
        [
            claim(
                "control.state",
                "confirmed",
                source_type="obs.structured.target_values",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "business.outcome",
                "contradicted",
                source_type="adapter.validation_error",
                scope="row:7",
                evidence="the persistence endpoint rejected the value",
                authoritative=True,
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "contradicted"


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
