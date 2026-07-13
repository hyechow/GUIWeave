import json
from pathlib import Path

from gui_agent.core.run.mutation import authorize_mutation, resolve_mutation
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult


REPLAYS = Path(__file__).resolve().parents[1] / "evals/browser/supervisor_replay"
FIXTURE = REPLAYS / "142444_mutation_subject"
CHOICE_FIXTURE = REPLAYS / "152920_choice_surface"


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


def _fixture() -> tuple[Milestone, list[PolicyTurn], dict]:
    raw = json.loads((FIXTURE / "context.json").read_text())
    turns = [PolicyTurn.model_validate(item) for item in raw["turns"]]
    milestone_id = turns[5].supervisor.milestone_id
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
            "mutation_mode",
            "requires_commit",
            "target_controls",
            "target_values",
        )
        if field in statement
    })
    merged["kind"] = statement.get("kind") or statement.get("run_kind") or base["kind"]
    merged["status"] = "running"
    expectation = json.loads((FIXTURE / "replay_expectation.json").read_text())
    return Milestone.model_validate(merged), turns, expectation


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

    milestone = Milestone(
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
    for turn_no in (24, 25, 26):
        observation = _observation(CHOICE_FIXTURE, turn_no)
        derived = active_choice_controls(observation, milestone.target_values)
        normalized = policy._mutation_observation(  # noqa: SLF001 - replay the policy seam
            observation,
            milestone.target_values,
        )
        subject = resolve_mutation(milestone, normalized, [])
        states[turn_no] = subject.status
        step = policy._plan_single(  # noqa: SLF001 - replay the production policy seam
            milestone,
            _SingleCheckResult(
                status="in_progress",
                reason="the exact declared choices are not ready",
                summary="choice preparation is pending",
                outcome_status="contradicted",
            ),
            normalized,
            [],
        )

        assert len(derived) == 33
        assert not any(
            item["option_text"] in {"Next", "Select All", "Deselect All", "Remove Attribute"}
            for item in derived
        )
        if turn_no < 26:
            assert step.atomic_role == "prepare"
            assert step.requires_mutation_authorization is False
        else:
            assert step.atomic_role == "write"
            assert step.mutation_authorization is not None
            assert step.mutation_authorization.source == "visual"
            assert step.mutation_authorization.subject_ref.startswith("choice:dialog:")

    assert states == {24: "preparing", 25: "preparing", 26: "writable"}
    assert subject.target_control == "Color green"
    assert subject.source == "visual"
