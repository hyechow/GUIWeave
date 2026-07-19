"""Phase 2: CollectionView is injected into the Transition decision packet for list[record]
collect-Interacts, the packet exposes no state-machine control surface, and a complete
proposal may cite a collection-bearing journal turn.
"""

from __future__ import annotations

import io
import json

from PIL import Image

from gui_agent.core.run.collection_view import CollectionSliceEvent
from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.run.statement_transition import validate_evidence_references
from gui_agent.core.schemas import (
    CollectionProvenance,
    Observation,
    OutputSpec,
    StatementContract,
)
from gui_agent.core.supervisor.statement import policy as policy_module
from gui_agent.core.supervisor.statement.model_io import _transition_frame_block
from gui_agent.core.supervisor.statement.observation_view import build_observation_view
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.core.supervisor.statement.schemas import (
    _StatementTransitionResult,
    _TransitionAction,
    _TransitionAssessment,
    _TransitionEvidence,
)


INSTANCE = "run:s1"


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(stream, format="PNG")
    return stream.getvalue()


def _observation(**updates) -> Observation:
    return Observation.model_validate({
        "png_bytes": _png(),
        "source": "browser",
        "title": "Grid",
        **updates,
    })


def _act_decision() -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="in_progress",
            summary="still collecting",
            established_facts=[],
            open_gaps=["more pages remain"],
            last_action_effect="none",
        ),
        kind="act",
        reason="collect more",
        action=_TransitionAction(
            instruction="click next page",
            atomic_role="iterate",
            action_family="iterate",
            target_control="Next page",
            expected_result="next page of records appears",
        ),
    )


def _list_record_contract(*, coverage: str = "current_view") -> StatementContract:
    return StatementContract(
        id="s1",
        goal="collect all matching rows",
        success="all reachable rows observed",
        returns={"rows": OutputSpec(type="list[record]", coverage=coverage)},  # type: ignore[arg-type]
    )


def _complete_decision(*, event_ref: str = "collection:1") -> _StatementTransitionResult:
    return _StatementTransitionResult(
        assessment=_TransitionAssessment(
            status="satisfied",
            summary="all rows observed",
            established_facts=["reached collection boundary"],
            open_gaps=[],
            last_action_effect="effective",
        ),
        kind="complete",
        reason="collected all reachable records",
        evidence=[
            _TransitionEvidence(
                source="journal",
                event_ref=event_ref,
                claim="已到达集合终点，累计 N 条覆盖全部记录",
            )
        ],
    )


def _collection_turn(
    index: int,
    *,
    boundary: str,
    known_total: int | None = None,
    records: list[dict] | None = None,
    content_key: str = "k1",
) -> CollectionSliceEvent:
    rows = records if records is not None else [{"id": "1"}]
    return CollectionSliceEvent(
        event_ref=f"collection:{index}",
        after_turn=max(0, index - 1),
        statement_instance_id=INSTANCE,
        statement_id="s1",
        frame_ref=f"frame:{index}",
        collection_key="grid",
        provenance=CollectionProvenance(
            surface_fingerprint="table:grid",
            schema_fingerprint="schema",
            route="/grid",
        ),
        records=rows,
        known_total=known_total,
        boundary=boundary,  # type: ignore[arg-type]
        content_key=content_key,
        source="table",
    )


def _text_contract() -> StatementContract:
    return StatementContract(
        id="s1",
        goal="read the title",
        success="title read",
        returns={"title": OutputSpec(type="text")},
    )


def test_collection_view_passed_only_for_list_record_returns(monkeypatch) -> None:
    captured: dict = {}

    def capture(*_args, **kwargs):
        captured.update(kwargs)
        return _act_decision()

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)

    # list[record] contract -> collection_view is built and forwarded (non-None).
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_list_record_contract(), instance_id=INSTANCE)
    monkeypatch.setattr(policy, "_invoke_statement_transition", capture)
    policy._run_single_turn(_list_record_contract(), _observation(), [])
    assert captured.get("collection_view") is not None

    # text contract -> no collection projection, kwarg is None.
    captured.clear()
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_text_contract(), instance_id=INSTANCE)
    monkeypatch.setattr(policy, "_invoke_statement_transition", capture)
    policy._run_single_turn(_text_contract(), _observation(), [])
    assert captured.get("collection_view") is None


def test_collection_block_carries_facts_and_no_control_surface() -> None:
    contract = _list_record_contract()
    turn = _collection_turn(
        3, boundary="at_end", known_total=2,
        records=[{"id": "1"}, {"id": "2"}], content_key="k3",
    )
    from gui_agent.core.run.collection_view import build_collection_view

    view = build_collection_view(
        instance_id=INSTANCE, contract=contract, history=[turn],
    )
    block = _transition_frame_block(
        contract,
        _observation(),
        build_memory_view(instance_id=INSTANCE, contract=contract, history=[]),
        build_observation_view(contract, _observation(), []),
        initial_filters=None,
        collection_view=view,
    )
    payload = json.loads(block.content[block.content.index("{") :])
    assert "collection" in payload
    collection = payload["collection"]
    assert collection["record_count"] == 2
    assert collection["known_total"] == 2
    assert collection["coverage_status"] == "complete"
    # Forbidden control surface — CollectionView is a projection, not a state machine.
    for forbidden in ("advance", "next_action", "should_continue", "is_complete", "phase"):
        assert forbidden not in collection
        assert forbidden not in payload


def test_complete_proposal_cites_independent_collection_event(monkeypatch) -> None:
    """An independent collection slice is a citable Journal fact."""
    contract = _list_record_contract()
    turn = _collection_turn(1, boundary="at_end", known_total=1)
    complete = _complete_decision()
    verdict = validate_evidence_references(
        complete.evidence, available_refs={"collection:1"},
    )
    assert verdict.allowed, verdict.reason

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=INSTANCE)
    monkeypatch.setattr(policy, "_invoke_statement_transition", lambda *a, **k: complete)
    step = policy._run_single_turn(contract, _observation(), [turn])
    assert step.outcome is not None and step.outcome.phase == "completed"


# --- P0: mechanical complete-coverage gate ----------------------------------


def test_complete_rejected_when_coverage_incomplete(monkeypatch) -> None:
    """P0: has_next_page must mechanically veto complete for coverage=complete returns."""
    contract = _list_record_contract(coverage="complete")
    history = [_collection_turn(1, boundary="has_next_page", known_total=5, records=[{"id": "1"}])]
    calls: list[str] = []

    def invoke(*_a, **_k):
        calls.append("complete")
        return _complete_decision()

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=INSTANCE)
    monkeypatch.setattr(policy, "_invoke_statement_transition", invoke)
    # validation_retries=0: first rejection becomes transition failure (no re-decision loop).
    step = policy._run_single_turn(
        contract, _observation(), history, validation_retries=0,
    )
    assert step.outcome is not None
    assert step.outcome.phase != "completed"
    assert "coverage_status=incomplete" in (step.outcome.summary or step.summary)
    assert calls == ["complete"]


def test_complete_rejected_when_coverage_unknown(monkeypatch) -> None:
    """P0: no boundary / total evidence must not allow a confirmed complete claim."""
    contract = _list_record_contract(coverage="complete")
    history = [_collection_turn(1, boundary="unknown", records=[{"id": "1"}])]

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=INSTANCE)
    monkeypatch.setattr(
        policy, "_invoke_statement_transition", lambda *a, **k: _complete_decision(),
    )
    step = policy._run_single_turn(
        contract, _observation(), history, validation_retries=0,
    )
    assert step.outcome is not None
    assert step.outcome.phase != "completed"
    assert "coverage_status=unknown" in (step.outcome.summary or step.summary)


def test_complete_rejected_when_coverage_conflicting(monkeypatch) -> None:
    """P0: collected rows > known_total is conflicting and must veto complete."""
    contract = _list_record_contract(coverage="complete")
    history = [
        _collection_turn(
            1,
            boundary="at_end",
            known_total=1,
            records=[{"id": "1"}, {"id": "2"}],
        ),
    ]

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=INSTANCE)
    monkeypatch.setattr(
        policy, "_invoke_statement_transition", lambda *a, **k: _complete_decision(),
    )
    step = policy._run_single_turn(
        contract, _observation(), history, validation_retries=0,
    )
    assert step.outcome is not None
    assert step.outcome.phase != "completed"
    assert "coverage_status=conflicting" in (step.outcome.summary or step.summary)


def test_complete_accepted_when_coverage_complete(monkeypatch) -> None:
    """P0 positive: at_end + known_total met allows complete to pass the mechanical gate."""
    contract = _list_record_contract(coverage="complete")
    history = [
        _collection_turn(
            1, boundary="at_end", known_total=1, records=[{"id": "1"}], content_key="k1",
        ),
    ]

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=INSTANCE)
    monkeypatch.setattr(
        policy, "_invoke_statement_transition", lambda *a, **k: _complete_decision(),
    )
    step = policy._run_single_turn(contract, _observation(), history)
    assert step.outcome is not None
    assert step.outcome.phase == "completed"
    assert step.outcome.verification == "confirmed"


def test_incomplete_complete_proposal_redecides_once(monkeypatch) -> None:
    """Rejected complete feeds a constraint and re-decides once on the same frame."""
    contract = _list_record_contract(coverage="complete")
    history = [_collection_turn(1, boundary="has_next_page", known_total=3)]
    calls: list[str] = []

    def invoke(*_a, **_k):
        calls.append("call")
        return _complete_decision()

    monkeypatch.setattr(policy_module, "is_loading_frame", lambda _o: False)
    policy = StatementSupervisorPolicy()
    policy.begin_statement(contract, instance_id=INSTANCE)
    monkeypatch.setattr(policy, "_invoke_statement_transition", invoke)
    step = policy._run_single_turn(
        contract, _observation(), history, validation_retries=1,
    )
    assert len(calls) == 2  # original + one re-decision
    assert step.outcome is not None
    assert step.outcome.phase != "completed"
