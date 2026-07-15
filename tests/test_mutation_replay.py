import json
from pathlib import Path

from gui_agent.core.run.mutation import authorize_mutation, resolve_mutation
from gui_agent.core.run.progress_monitor import ProgressMonitor
from gui_agent.core.run.persistence import assess_persistence
from gui_agent.core.schemas import (
    ActionSignal,
    StatementContract,
    MutationReceipt,
    Observation,
    PolicyTurn,
    SupervisorStep,
)
from gui_agent.core.supervisor.milestone.schemas import _PlanResult, _SingleCheckResult
from gui_agent.core.supervisor.milestone.evidence import (
    action_lifecycle_claims,
)


REPLAYS = Path(__file__).resolve().parents[1] / "evals/browser/supervisor_replay"
FIXTURE = REPLAYS / "142444_mutation_subject"
CHOICE_FIXTURE = REPLAYS / "152920_choice_surface"
INTERMEDIATE_FIXTURE = REPLAYS / "205258_intermediate_transition"
NESTED_COMMIT_FIXTURE = REPLAYS / "091305_nested_commit"
DIRECT_SAVE_FIXTURE = REPLAYS / "105939_turn12"
PERSISTENCE_FLOW_FIXTURE = REPLAYS / "112455_persistence_flow"
TERMINAL_FRONTIER_FIXTURE = REPLAYS / "111415_terminal_frontier"
UNMET_PROGRESS_FIXTURE = REPLAYS / "143530_unmet_progress"


def _run_statements(node: object) -> list[dict]:
    statements: list[dict] = []
    if isinstance(node, dict):
        if node.get("op") == "run":
            statements.append(node)
        for value in node.values():
            statements.extend(_run_statements(value))
    elif isinstance(node, list):
        for value in node:
            statements.extend(_run_statements(value))
    return statements


def _context(root: Path, turn_no: int) -> tuple[StatementContract, list[PolicyTurn]]:
    raw = json.loads((root / "context.json").read_text())
    turns = []
    for item in raw["turns"]:
        supervisor = {
            key: value
            for key, value in item["supervisor"].items()
            if key in SupervisorStep.model_fields
        }
        turns.append(PolicyTurn.model_validate({**item, "supervisor": supervisor}))
    milestone_id = next(
        turn.supervisor.milestone_id for turn in turns if turn.index == turn_no
    )
    base = next(item for item in raw["milestones"] if item["id"] == milestone_id)
    statement = next(
        item
        for item in _run_statements(raw["orchestrator"])
        if item.get("name") == base["name"]
    )
    merged = dict(base)
    merged.update({
        field: statement[field]
        for field in (
            "effect_mode",
            "persistence",
            "target_controls",
            "target_values",
        )
        if field in statement
    })
    merged["kind"] = statement.get("kind") or statement.get("run_kind") or base["kind"]
    return StatementContract.model_validate(merged), turns


def _fixture() -> tuple[StatementContract, list[PolicyTurn], dict]:
    milestone, turns = _context(FIXTURE, 6)
    expected = json.loads((FIXTURE / "replay_expectation.json").read_text())
    return milestone, turns, expected


def _observation(root: Path, turn_no: int) -> Observation:
    payload = json.loads(
        (root / f"observation_turn_{turn_no}.json").read_text()
    )
    return Observation.model_validate({
        **payload["observation"],
        "png_bytes": b"structured-replay",
    })


def test_real_142444_context_never_authorizes_the_populated_existing_member() -> None:
    milestone, turns, expected = _fixture()

    for turn_no in (6, 7, 8):
        observation = _observation(FIXTURE, turn_no)
        subject = resolve_mutation(
            milestone,
            observation,
            turns[: turn_no - 1],
        )
        assert subject.status == expected[f"turn_{turn_no}"]
        recorded_step = turns[turn_no - 1].supervisor
        assert recorded_step is not None
        assert authorize_mutation(milestone, subject) is None
        assert subject.subject_ref != expected["forbidden_subject"]


def test_real_152920_choice_surface_resolves_cleanup_then_target_write() -> None:
    from gui_agent.adapters.browser.target_binding import active_choice_controls
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    milestone = StatementContract(
        id="m8_action",
        name="add one configuration combination",
        description="",
        success_condition="the saved collection contains green and XXXL",
        kind="action",
        target_values={"Color": "green", "Size": "XXXL"},
    )
    policy = MilestoneSupervisorPolicy(
        mutation_control_resolver=active_choice_controls,
    )
    states: dict[int, str] = {}
    targets: dict[int, str] = {}
    for turn_no in (24, 25, 26):
        observation = _observation(CHOICE_FIXTURE, turn_no)
        derived = active_choice_controls(observation, milestone.target_values)
        normalized = policy._mutation_observation(  # noqa: SLF001 - replay the policy seam
            observation,
            milestone.target_values,
        )
        subject = resolve_mutation(milestone, normalized, [])
        states[turn_no] = subject.status
        targets[turn_no] = subject.target_control
        step = policy._plan_single(  # noqa: SLF001 - replay the production policy seam
            milestone,
            _SingleCheckResult(
                status="in_progress",
                reason="the exact declared choices are not ready",
                summary="choice preparation is pending",
                effect_status="unmet",
            ),
            normalized,
            [],
        )

        assert len(derived) == 33
        assert [
            item["choice_operations"]
            for item in derived
            if item.get("choice_operations")
        ] == [
            {"select_all": "Select All", "clear_all": "Deselect All"},
            {"select_all": "Select All", "clear_all": "Deselect All"},
        ]
        if turn_no < 26:
            assert step.atomic_role == "prepare"
            assert step.requires_mutation_authorization is False
        else:
            assert step.atomic_role == "write"
            assert step.mutation_authorization is not None
            assert step.mutation_authorization.source == "visual"
            assert step.mutation_authorization.subject_ref.startswith("choice:dialog:")

    assert states == {24: "preparing", 25: "preparing", 26: "writable"}
    assert targets == {
        24: "Size Deselect All",
        25: "Color Deselect All",
        26: "Color green",
    }
    assert subject.target_control == "Color green"
    assert subject.source == "visual"


def test_real_choice_surface_executes_multi_value_contract_as_exact_set() -> None:
    from gui_agent.adapters.browser.target_binding import active_choice_controls

    milestone = StatementContract(
        id="multi-choice",
        name="add two configuration combinations",
        description="",
        success_condition="the saved collection contains XXXL-blue and XXXL-purple",
        kind="action",
        target_values={"Size": "XXXL", "Color": ["Blue", "Purple"]},
    )
    source = _observation(CHOICE_FIXTURE, 26)
    derived = list(active_choice_controls(source, milestone.target_values))
    assert len(derived) == 33

    def state(*selected: str):
        selected_keys = {value.casefold() for value in selected}
        controls = [
            {
                **control,
                "checked": str(control.get("option_text", "")).casefold() in selected_keys,
                "value": (
                    "on"
                    if str(control.get("option_text", "")).casefold() in selected_keys
                    else "off"
                ),
            }
            for control in derived
        ]
        return resolve_mutation(
            milestone,
            Observation(png_bytes=b"replay", source="browser", form_controls=controls),
            [],
        )

    assert (state().status, state().target_control) == ("writable", "Size XXXL")
    assert (state("XXXL").status, state("XXXL").target_control) == (
        "writable", "Color Blue",
    )
    assert (state("XXXL", "Blue").status, state("XXXL", "Blue").target_control) == (
        "writable", "Color Purple",
    )
    assert state("XXXL", "Blue", "Purple").status == "complete"
    assert state("XXXL", "Blue", "Purple", "Green").status == "preparing"
    assert resolve_mutation(
        milestone, Observation(png_bytes=b"visual-only", source="browser"), []
    ).status == "unknown"


def test_real_205258_completed_choice_set_keeps_intermediate_transition_prepare() -> None:
    from gui_agent.adapters.browser.target_binding import (
        active_choice_controls,
        active_surface_id,
        active_target_aliases,
    )
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    milestone = StatementContract(
        id="m9_action",
        name="persist the declared configuration combinations",
        description="",
        success_condition="the saved collection contains both declared combinations",
        kind="action",
        target_values={"Size": "XXS", "Color": ["blue", "purple"]},
        persistence="explicit_commit",
    )
    observation = _observation(INTERMEDIATE_FIXTURE, 28)
    policy = MilestoneSupervisorPolicy(
        active_target_resolver=active_target_aliases,
        mutation_control_resolver=active_choice_controls,
        surface_resolver=active_surface_id,
    )
    normalized = policy._mutation_observation(  # noqa: SLF001 - replay production seam
        observation,
        milestone.target_values,
    )
    subject = resolve_mutation(
        milestone,
        normalized,
        [],
        surface_id=active_surface_id(observation),
    )
    history = [
        PolicyTurn(
            index=27,
            observation_source="browser",
            supervisor=SupervisorStep(
                should_act=True,
                instruction="select the final declared choice",
                summary="",
                milestone_id=milestone.id,
                atomic_role="write",
                target_control="Size XXS",
                target_value="XXS",
            ),
            executed=True,
            action_signal=ActionSignal(
                role="write",
                target_control="Size XXS",
                target_value="XXS",
                execution="dispatched",
                target="on_target",
                response="observed",
                surface_id=active_surface_id(observation),
                mutation_receipt=MutationReceipt(
                    statement_id=milestone.id,
                    subject_ref=subject.subject_ref,
                    field="Size",
                    intended_value="XXS",
                    source="structural",
                ),
            ),
        )
    ]
    proposal = _PlanResult(
        instruction="advance the current workflow",
        summary="the local choices are complete; continue to the next workflow surface",
        atomic_role="prepare",
        action_family="activate",
        target_control="Next",
    )
    policy._invoke_planner = lambda *_args, **_kwargs: proposal  # type: ignore[method-assign]

    step = policy._plan_single(  # noqa: SLF001 - replay production policy seam
        milestone,
        _SingleCheckResult(
            status="in_progress",
            reason="declared choices are complete but the workflow has not reached persistence",
            summary="continue the workflow",
            effect_status="unverified",
        ),
        normalized,
        history,
    )

    assert subject.status == "complete"
    assert step.should_act is True
    assert step.atomic_role == "prepare"
    assert step.target_control == "Next"


def test_real_091305_child_dispatch_does_not_consume_terminal_commit() -> None:
    milestone, turns = _context(NESTED_COMMIT_FIXTURE, 34)
    target_turn = next(turn for turn in turns if turn.index == 34)
    history = [turn for turn in turns if turn.index < 34]
    scope = target_turn.supervisor.execution_scope
    claims = action_lifecycle_claims(
        milestone,
        history,
        scope=scope,
    )
    execution = next(item for item in claims if item.domain == "action.execution")
    persistence = assess_persistence(milestone, history, scope=scope)

    assert persistence.status == "pending"
    assert persistence.terminal_turn is None
    assert execution.source_type == "runtime.action_dispatch"


def test_real_111415_returned_child_commit_requires_root_commit(monkeypatch) -> None:
    from gui_agent.adapters.browser.target_binding import active_surface_id
    from gui_agent.core.supervisor.milestone import policy as policy_module
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    milestone, turns = _context(TERMINAL_FRONTIER_FIXTURE, 32)
    history = [turn for turn in turns if turn.index < 32]
    observation = _observation(TERMINAL_FRONTIER_FIXTURE, 32)
    prior = _observation(TERMINAL_FRONTIER_FIXTURE, 31)
    recorded = next(turn for turn in turns if turn.index == 32)
    persistence = assess_persistence(
        milestone,
        history,
        scope=recorded.supervisor.execution_scope,
        current_surface=active_surface_id(observation),
    )
    assert persistence.terminal_ready

    extras: list[str] = []
    policy = MilestoneSupervisorPolicy(surface_resolver=active_surface_id)
    policy.begin_statement(milestone, instance_id="test:mutation")
    policy._monitor._last_url = prior.url
    policy._monitor._last_dom_state = prior.dom_state
    policy._single_check = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        _SingleCheckResult.model_validate(recorded.checker).model_copy(update={
            "status": "stuck",
            "effect_status": "unmet",
            "reason": "draft rows are not final before the root commit",
        })
    )

    plans = iter((
        _PlanResult(
            instruction="reopen Edit Configurations",
            summary="repeat the child flow",
            atomic_role="prepare",
            action_family="activate",
            target_control="Edit Configurations",
        ),
        _PlanResult(
            instruction="click the root Save",
            summary="persist the resource",
            atomic_role="commit",
            action_family="activate",
            target_control="Save",
        ),
    ))

    def plan(*_args, **kwargs):
        extras.append(kwargs.get("extra", ""))
        return next(plans)

    policy._invoke_planner = plan  # type: ignore[method-assign]
    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _observation: False)

    def unexpected_similarity(*_args):
        raise AssertionError("terminal commit frontier must bypass route-level stuck detection")

    policy._monitor.check_screen_similarity = unexpected_similarity  # type: ignore[method-assign]

    step = policy._run_single_turn(milestone, observation, history)

    assert step.should_act is True
    assert step.atomic_role == "commit"
    assert step.target_control == "Save"
    assert len(extras) == 2
    assert all("atomic_role=commit" in extra for extra in extras)


def test_real_143530_unmet_frames_do_not_consume_recovery_retries(
    monkeypatch,
) -> None:
    from gui_agent.core.supervisor.milestone import policy as policy_module
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy

    for turn_no in (9, 10, 11):
        milestone, turns = _context(UNMET_PROGRESS_FIXTURE, turn_no)
        history = [turn for turn in turns if turn.index < turn_no]
        observation = _observation(UNMET_PROGRESS_FIXTURE, turn_no)
        prior = _observation(UNMET_PROGRESS_FIXTURE, turn_no - 1)
        recorded = next(turn for turn in turns if turn.index == turn_no)
        check = _SingleCheckResult.model_validate(recorded.checker)
        assert check.effect_status == "unmet"

        policy = MilestoneSupervisorPolicy()
        policy.begin_statement(milestone, instance_id=f"test:mutation:{turn_no}")
        policy._monitor._last_url = prior.url
        policy._monitor._last_dom_state = prior.dom_state
        policy._single_check = lambda *_args, _check=check, **_kwargs: _check  # type: ignore[method-assign]
        policy._invoke_planner = lambda *_args, _turn=turn_no, **_kwargs: (  # type: ignore[method-assign]
            _PlanResult(
                instruction=(
                    "click Add Swatch"
                    if _turn == 11
                    else "scroll toward the option collection boundary"
                ),
                summary="continue the current acquire path",
                atomic_role="prepare" if _turn == 11 else "iterate",
                action_family="activate" if _turn == 11 else "navigate",
                target_control="Add Swatch" if _turn == 11 else "option_collection",
                direction=None if _turn == 11 else "down",
            )
        )
        policy._invoke_replanner = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
            (_ for _ in ()).throw(AssertionError("ordinary unmet state triggered recovery"))
        )
        monkeypatch.setattr(policy_module, "is_loading_frame", lambda _observation: False)

        step = policy._run_single_turn(milestone, observation, history)

        assert step.should_act is True
        assert step.outcome is None
        assert policy._rt.retry_count == 0
        if turn_no == 11:
            assert step.target_control == "Add Swatch"


def test_real_persistence_boundaries_share_one_progress_projection() -> None:
    cases = (
        (PERSISTENCE_FLOW_FIXTURE, 29, "pending", None),
        (PERSISTENCE_FLOW_FIXTURE, 30, "submitted", 30),
        (DIRECT_SAVE_FIXTURE, 11, "submitted", 11),
    )
    for root, turn_no, expected_status, terminal_index in cases:
        milestone, turns = _context(root, turn_no)
        target_turn = next(turn for turn in turns if turn.index == turn_no)
        history = [turn for turn in turns if turn.index <= turn_no]
        persistence = assess_persistence(
            milestone,
            history,
            scope=target_turn.supervisor.execution_scope,
        )

        assert persistence.status == expected_status
        assert (
            persistence.terminal_turn.index
            if persistence.terminal_turn is not None
            else None
        ) == terminal_index
