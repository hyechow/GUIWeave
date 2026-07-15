from gui_agent.core.run.execution_signals import (
    ExecutionCoordinator,
    ConstraintLedger,
    ExecutionContract,
    claim,
)
from gui_agent.core.run.persistence import PersistenceAssessment, assess_persistence
from gui_agent.core.schemas import (
    ActionSignal,
    Milestone,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone.schemas import action_metadata
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult


def _signal_turn(
    index: int,
    *,
    role: str,
    control: str,
    surface: str = "",
    milestone_id: str = "persisted",
    field: str = "",
    value: str = "",
) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="test",
        supervisor=SupervisorStep(
            should_act=True,
            instruction=control,
            summary="",
            milestone_id=milestone_id,
            atomic_role=role,
            target_control=control,
        ),
        executed=True,
        action_signal=ActionSignal(
            role=role,
            surface_id=surface,
            target_control=control,
            mutation_receipt=(
                MutationReceipt(
                    statement_id=milestone_id,
                    subject_ref=f"choice:{surface}",
                    field=field,
                    intended_value=value,
                    source="visual",
                )
                if field
                else None
            ),
            execution="dispatched",
            target="on_target",
            response="observed",
        ),
    )


def _redirected_mutation_contract() -> ExecutionContract:
    return ExecutionContract(
        statement_id="save",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
        completion_mode="mutation",
    )


def test_unmet_state_remains_actionable_without_progress_failure() -> None:
    scope = "milestone:add-value"
    decision = ExecutionCoordinator().decide(
        _redirected_mutation_contract(),
        [
            claim(
                "control.state",
                "unmet",
                source_type="obs.mutation.desired_state",
                scope=scope,
                authoritative=True,
                evidence="the declared value is not present yet",
            )
        ],
        scope=scope,
    )

    assert decision.status == "pending"
    assert decision.next == "act"


def test_destination_page_state_cannot_contradict_source_resource_mutation() -> None:
    scope = "milestone:save"
    decision = ExecutionCoordinator().decide(
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
                    "unmet",
                source_type="obs.dom.target_values",
                scope=scope,
                subject_scope="milestone:save",
                evidence="destination list has no source form controls",
                authoritative=True,
            ),
            claim(
                "effect.state",
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
    decision = ExecutionCoordinator().decide(
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
                "effect.state",
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


def test_persisted_mutation_keeps_transaction_role_separate_from_ui_family():
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
            persistence="explicit_commit",
            effect_mode="transform",
            target_values={"member": "target"},
        ),
    )

    assert role == "commit"
    assert family == "activate"


def test_persisted_mutation_projects_pending_commit_without_rejecting_proposals():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
    )
    history = [
        _signal_turn(1, role="prepare", control="Open editor", surface="parent"),
        _signal_turn(2, role="write", control="Target value", surface="child:values"),
        _signal_turn(3, role="prepare", control="Next", surface="child:values"),
        _signal_turn(4, role="commit", control="Generate draft", surface="child:summary"),
    ]
    state = assess_persistence(milestone, history)
    assert state.status == "pending"
    assert state.latest_write is history[1]
    assert state.entry_surface == "parent"


def test_persistence_projection_does_not_compare_control_names():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
    )
    history = [
        _signal_turn(1, role="prepare", control="Next", surface="wizard:step-1"),
        _signal_turn(2, role="write", control="Target A", surface="wizard:step-2"),
    ]
    state = assess_persistence(milestone, history)
    assert state.status == "pending"
    assert state.entry_surface == "wizard:step-1"


def test_pending_persistence_without_terminal_readiness_allows_forward_prepare():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child editor", surface="parent"),
        _signal_turn(2, role="write", control="Target A", surface="parent"),
        _signal_turn(3, role="prepare", control="Generate draft", surface="parent"),
    ]
    proposals = iter([
        _PlanResult(
            instruction="open the child editor again",
            summary="re-enter child flow",
            atomic_role="prepare",
            action_family="activate",
            target_control="Open child editor",
        ),
        _PlanResult(
            instruction="save the current resource",
            summary="cross the pending boundary",
            atomic_role="commit",
            action_family="activate",
            target_control="Save",
        ),
    ])
    policy = MilestoneSupervisorPolicy(surface_resolver=lambda _observation: "parent")
    policy._invoke_planner = lambda *_args, **_kwargs: next(proposals)  # type: ignore[method-assign]

    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="state not yet durable",
            summary="visual-only parent surface",
            effect_status="unverified",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        history,
    )

    assert step.atomic_role == "prepare"
    assert step.target_control == "Open child editor"


def test_unknown_visual_surface_does_not_suppress_an_ordinary_proposal():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
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
            effect_status="unverified",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        history,
    )

    assert step.should_act is True
    assert step.instruction == "open the child editor again"


def test_terminal_ready_rejects_repeated_noncommit_proposals():
    milestone = Milestone(
        id="persisted",
        name="update collection and save",
        description="",
        success_condition="saved collection contains target rows",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child", surface="parent"),
        _signal_turn(2, role="write", control="Target A", surface="child"),
        _signal_turn(3, role="commit", control="Generate draft", surface="child"),
    ]
    regression = _PlanResult(
        instruction="open the child editor again",
        summary="re-enter child flow",
        atomic_role="prepare",
        action_family="activate",
        target_control="Open child editor",
    )
    policy = MilestoneSupervisorPolicy(surface_resolver=lambda _observation: "parent")
    policy._invoke_planner = lambda *_args, **_kwargs: regression  # type: ignore[method-assign]

    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="root commit is pending",
            summary="returned to parent",
            effect_status="unverified",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        history,
    )

    assert step.should_act is False
    assert step.atomic_role == "commit"
    assert "non-commit proposal rejected" in step.summary
    assert any(
        "terminal persistence is pending" in item
        for item in policy.constraints_snapshot("milestone:persisted")
    )


def test_complete_write_receipts_keep_child_workflow_moving_forward():
    milestone = Milestone(
        id="persisted",
        name="materialize declared choices and save",
        description="",
        success_condition="saved collection contains the declared choices",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
        target_values={"Size": "XXXL", "Color": "green"},
    )
    history = [
        _signal_turn(1, role="prepare", control="Open child", surface="parent"),
        _signal_turn(
            2, role="write", control="XXXL", surface="child", field="Size", value="XXXL"
        ),
        _signal_turn(
            3, role="write", control="Green", surface="child", field="Color", value="green"
        ),
        _signal_turn(4, role="prepare", control="Next", surface="child"),
    ]
    extras: list[str] = []

    def planner(*_args, **kwargs):
        extras.append(kwargs.get("extra", ""))
        return _PlanResult(
            instruction="generate the prepared child records",
            summary="continue from completed target writes",
            atomic_role="commit",
            action_family="activate",
            target_control="Generate",
        )

    policy = MilestoneSupervisorPolicy(surface_resolver=lambda _observation: "child-summary")
    policy._invoke_planner = planner  # type: ignore[method-assign]
    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="effect remains unverified",
            summary="later child workflow surface",
            effect_status="unverified",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        history,
    )

    assert step.atomic_role == "commit"
    assert step.target_control == "Generate"
    assert extras and "write receipts" in extras[0]


def test_workflow_command_mislabeled_as_write_is_reclassified_as_preparation():
    milestone = Milestone(
        id="persisted",
        name="prepare one declared combination and save",
        description="",
        success_condition="saved collection contains the declared combination",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
        target_values={"Color": "green", "Size": "XXXL"},
    )
    proposal = _PlanResult(
        instruction="clear the current draft selection",
        summary="reduce the draft frontier before selecting the target",
        atomic_role="write",
        action_family="activate",
        target_control="collection selection command",
        target_value="",
    )
    policy = MilestoneSupervisorPolicy()
    policy._invoke_planner = lambda *_args, **_kwargs: proposal  # type: ignore[method-assign]

    step = policy._plan_single(
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="the draft selection still contains extra values",
            summary="selection preparation is pending",
            effect_status="unmet",
        ),
        Observation(png_bytes=b"frame", source="visual"),
        [],
    )

    assert step.should_act is True
    assert step.atomic_role == "prepare"
    assert step.target_control == "collection selection command"
    assert step.mutation_authorization is None
    assert step.requires_mutation_authorization is False


def test_persistence_uses_structured_role_not_instruction_vocabulary():
    milestone = Milestone(
        id="m",
        name="perform mutation",
        description="",
        success_condition="state is durable",
        kind="action",
        effect_mode="dispatch",
        persistence="explicit_commit",
    )
    turn = _signal_turn(
        1,
        role="commit",
        control="trigger boundary",
        milestone_id="m",
    )

    attempted = assess_persistence(milestone, [turn])
    assert attempted.status == "pending"
    assert attempted.terminal_turn is turn
    assert turn.action_signal is not None
    turn.action_signal.response_channels.append("url")
    assert assess_persistence(milestone, [turn]).status == "submitted"
    prepare = _signal_turn(
        1,
        role="prepare",
        control="trigger boundary",
        milestone_id="m",
    )
    assert assess_persistence(milestone, [prepare]).status == "clean"


def test_persistence_does_not_treat_nested_commit_as_terminal():
    milestone = Milestone(
        id="persisted",
        name="persist mutation",
        description="",
        success_condition="state is durable",
        kind="action",
        persistence="explicit_commit",
        effect_mode="transform",
    )
    outer_prepare = _signal_turn(
        1,
        role="prepare",
        control="open child workflow",
        surface="resource:record-7",
    )
    child_commit = _signal_turn(
        3,
        role="commit",
        control="materialize child state",
        surface="child:workflow-1",
    )
    write = _signal_turn(2, role="write", control="target value", surface="child:workflow-1")
    outer_commit = _signal_turn(
        4,
        role="commit",
        control="persist resource",
        surface="resource:record-7",
    )

    nested = assess_persistence(milestone, [outer_prepare, child_commit])
    assert nested.status == "clean"
    assert nested.terminal_turn is None

    pending = assess_persistence(milestone, [outer_prepare, write, child_commit])
    assert pending.status == "pending"

    persisted = assess_persistence(
        milestone, [outer_prepare, write, child_commit, outer_commit]
    )
    assert persisted.status == "pending"
    assert persisted.terminal_turn is outer_commit
    assert outer_commit.action_signal is not None
    outer_commit.action_signal.response_channels.append("url")
    assert assess_persistence(
        milestone, [outer_prepare, write, child_commit, outer_commit]
    ).status == "submitted"

    redirected_child = child_commit.model_copy(deep=True)
    assert redirected_child.action_signal is not None
    redirected_child.action_signal.response_channels.append("url")
    redirected = assess_persistence(milestone, [outer_prepare, write, redirected_child])
    assert redirected.status == "pending"
    assert redirected.terminal_turn is None


def test_filter_with_result_completes_when_applied_even_if_result_is_zero():
    contract = ExecutionContract(
        statement_id="filter",
        kind="filter",
        output_fields=("match_count",),
        completion_mode="filter_state_with_result",
    )
    decision = ExecutionCoordinator().decide(
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
        ],
        scope="milestone:filter",
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "confirmed"


def test_commit_dispatch_without_outcome_feedback_is_provisional():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
        completion_mode="mutation",
    )
    claims = [
        claim(
            "action.write", "confirmed",
            source_type="runtime.write_dispatch", scope="row:7", authoritative=True,
        ),
        claim(
            "action.execution", "confirmed",
            source_type="runtime.commit_dispatch", scope="row:7", authoritative=True,
        ),
        claim(
            "effect.state", "unverified",
            source_type="checker", scope="row:7",
        ),
    ]
    attempt = PersistenceAssessment(
        status="pending",
        terminal_turn=_signal_turn(
            2, role="commit", control="Save", milestone_id="save"
        ),
    )
    decision = ExecutionCoordinator().decide(
        contract,
        claims,
        scope="row:7",
        persistence_assessment=attempt,
    )

    assert decision.status == "pending"
    assert decision.completion_status == "in_progress"
    assert decision.next == "observe"

    dom_response = claim(
        "page.response", "confirmed",
        source_type="runtime.effect_monitor", scope="row:7", authoritative=True,
        coverage="in_place_transition",
    )
    assert ExecutionCoordinator().decide(
        contract,
        [*claims, dom_response],
        scope="row:7",
        persistence_assessment=attempt,
    ).next == "observe"

    acknowledged = PersistenceAssessment(
        status="submitted",
        terminal_turn=_signal_turn(
            2, role="commit", control="Save", milestone_id="save"
        ),
    )
    assert ExecutionCoordinator().decide(
        contract,
        claims,
        scope="row:7",
        persistence_assessment=acknowledged,
    ).completion_status == "accepted_unverified"


def test_commit_cannot_be_accepted_while_declared_target_values_are_incomplete():
    contract = ExecutionContract(
        statement_id="save",
        kind="action",
        effect_mode="transform",
        persistence="explicit_commit",
        completion_mode="mutation",
    )
    decision = ExecutionCoordinator().decide(
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
                "unmet",
                source_type="obs.dom.target_values",
                scope="row:7",
                evidence="declared fields are incomplete",
                authoritative=True,
            ),
            claim(
                "effect.state",
                "unverified",
                source_type="checker",
                scope="row:7",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "contradicted"
    assert decision.next == "recover"


def test_authoritative_field_state_overrides_checker_pixel_contradiction():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="save",
            kind="action",
            effect_mode="transform",
            persistence="explicit_commit",
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
                "effect.state",
                "contradicted",
                source_type="checker",
                scope="row:7",
                evidence="the screenshot appears to show placeholder text",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "pending"
    assert decision.next == "act"


def test_authoritative_business_failure_still_overrides_field_state():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="save",
            kind="action",
            effect_mode="transform",
            persistence="explicit_commit",
            completion_mode="mutation",
        ),
        [
            claim(
                "action.execution",
                "confirmed",
                source_type="runtime.commit_dispatch",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "control.state",
                "confirmed",
                source_type="obs.structured.target_values",
                scope="row:7",
                authoritative=True,
            ),
            claim(
                "effect.state",
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
        effect_mode="transform",
        persistence="explicit_commit",
        completion_mode="mutation",
    )
    decision = ExecutionCoordinator().decide(
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
                "effect.state",
                "confirmed",
                source_type="checker",
                scope="row:7",
                evidence="possibly stale success cue",
            ),
        ],
        scope="row:7",
    )

    assert decision.status == "pending"
    assert decision.next == "act"


def test_change_mutation_cannot_complete_from_commit_without_write():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="save",
            kind="action",
            persistence="explicit_commit",
            completion_mode="mutation",
            effect_mode="transform",
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
                "effect.state",
                "unverified",
                source_type="checker",
                scope="milestone:save",
            ),
        ],
        scope="milestone:save",
    )

    assert decision.status == "contradicted"
    assert decision.next == "recover"


def test_ensure_mutation_accepts_authoritative_preexisting_outcome():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="ensure",
            kind="action",
            completion_mode="mutation",
            effect_mode="ensure",
        ),
        [
            claim(
                "effect.state",
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
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="ensure_saved",
            kind="action",
            persistence="explicit_commit",
            completion_mode="mutation",
            effect_mode="ensure",
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
    assert decision.next == "act"


def test_ensure_mutation_with_preexisting_control_state_still_skips_commit():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="ensure_existing",
            kind="action",
            persistence="explicit_commit",
            completion_mode="mutation",
            effect_mode="ensure",
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
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="ensure",
            kind="action",
            persistence="explicit_commit",
            completion_mode="mutation",
            effect_mode="ensure",
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

    assert decision.status == "contradicted"
    assert decision.next == "recover"


def test_checker_without_navigation_dispatch_cannot_complete_navigation():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="open",
            kind="navigation",
            completion_mode="arrival",
        ),
        [claim(
            "effect.state",
            "confirmed",
            source_type="checker",
            scope="milestone:open",
            evidence="checker says the destination is visible",
        )],
        scope="milestone:open",
    )

    assert decision.status == "pending"


def test_collection_requires_authoritative_complete_coverage():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="rows",
            kind="collection",
            completion_mode="read",
        ),
        [
            claim(
                "collection.coverage",
                "partial",
                source_type="runtime.collection_controller",
                scope="milestone:rows",
                authoritative=True,
                coverage="partial",
            )
        ],
        scope="milestone:rows",
    )

    assert decision.status == "pending"


def test_collection_completes_from_authoritative_coverage_fact():
    decision = ExecutionCoordinator().decide(
        ExecutionContract(
            statement_id="rows",
            kind="collection",
            completion_mode="read",
        ),
        [claim(
            "collection.coverage",
            "complete",
            source_type="runtime.collection_controller",
            scope="milestone:rows",
            evidence="observable boundary reached",
            authoritative=True,
            coverage="complete",
        )],
        scope="milestone:rows",
    )

    assert decision.status == "satisfied"
    assert decision.completion_status == "confirmed"


def test_runtime_constraint_is_visible_only_in_its_scope():
    ledger = ConstraintLedger()
    ledger.add("do not repeat this scroll", scope="milestone:first")

    assert ledger.visible("milestone:first") == ["do not repeat this scroll"]
    assert ledger.visible("milestone:second") == []
