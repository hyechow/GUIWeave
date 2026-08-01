from types import SimpleNamespace

from gui_agent.adapters.browser.acquisition import validate_collection_action
from gui_agent.core.run.contracts import Acquire, OutputSpec, StatementInvocation
from gui_agent.core.run.statements.acquire import (
    build_acquire_memory,
    execute_acquire_statement,
)
from gui_agent.core.run.statements.acquire_policy import AcquireDecision
from gui_agent.core.run.statements.observation import ObservationCursor
from gui_agent.core.schemas import BaseAction, BaseActionDecision, EventJournal, Observation, PolicyContext


def _observation(page, *, has_next=None, total=None, traversal=True):
    signal = (
        {
            "type": "paged",
            "page_index": page,
            "has_next_page": has_next,
            "has_prev_page": page > 1,
        }
        if traversal else None
    )
    table = {
        "path": "#records",
        "caption": "Records",
        "headers": ["ID", "Value"],
        "rows": [{"ID": str(page), "Value": f"v{page}"}],
    }
    if total is not None:
        table["total_records"] = total
    if signal is not None:
        table["traversal"] = signal
    return Observation(
        png_bytes=b"png",
        source="browser",
        url="https://example.test/list",
        tables=[table],
    )


def _invocation(coverage="complete"):
    statement = Acquire(
        id="collect",
        bind="rows",
        goal="collect every reachable record from the scoped collection",
        returns={
            "records": OutputSpec(
                type="list[record]",
                coverage=coverage,
                description="record identity and value",
            )
        },
    )
    return StatementInvocation(statement=statement)


def _lookup_scope(fields):
    return {
        "kind": "resolved_collection",
        "entity": "Records",
        "surface_fingerprint": "table:#records",
        "available_fields": fields,
    }


def _execute(invocation, tmp_path, bundle, observation=None):
    return execute_acquire_statement(
        invocation,
        cursor=ObservationCursor(
            bundle=bundle,
            platform=object(),
            log_dir=tmp_path,
            observation=observation,
            observation_url="frame.png" if observation else "",
        ),
        bundle=bundle,
        platform=object(),
        context=PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a"),
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )


class _Perception:
    def __init__(self, observation):
        self.observation = observation

    def observe(self):
        return self.observation


def _cell_observation(
    *contents: str,
    fingerprint: str = "android-collection:feed",
    y_offset: int = 0,
) -> Observation:
    return Observation(
        png_bytes=b"png",
        source="android",
        collection_regions=[{
            "ref": "android-collection:0",
            "surface_fingerprint": fingerprint,
            "bounds": (0, 0, 1000, 1000),
            "traversal": {"type": "scroll"},
            "cells": [
                {
                    "ref": f"android:0.{index}",
                    "structural_key": "row",
                    "content_key": content,
                    "texts": [content],
                    "bounds": (0, y_offset + index * 100, 1000, y_offset + (index + 1) * 100),
                }
                for index, content in enumerate(contents)
            ],
        }],
    )


def _cell_invocation() -> StatementInvocation:
    return StatementInvocation(
        statement=Acquire(
            id="collect",
            bind="rows",
            goal="collect values",
            required_fields=["value"],
            returns={"records": OutputSpec(type="list[record]", coverage="complete")},
        ),
        args={
            "lookup_scope": {
                "kind": "resolved_collection",
                "entity": "Records",
                "surface_fingerprint": "android-collection:feed",
                "available_fields": [],
                "projection": "cells",
            },
        },
    )


def _run_cell_acquire(monkeypatch, tmp_path, first, observations):
    moves = []
    paths = []

    def observe(_platform, path):
        paths.append(path.name)
        return _Perception(observations.pop(0))

    bundle = SimpleNamespace(
        make_perception=observe,
        move_collection=lambda _platform, _table, family: moves.append(family) or True,
    )
    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.materialize_cell_records",
        lambda cells, _fields: [{"value": cell["texts"][0]} for cell in cells],
    )
    return _execute(_cell_invocation(), tmp_path, bundle, first), moves, paths


def test_cell_acquire_owns_stale_retry_boundary_and_returns_only_rows(
    monkeypatch, tmp_path,
) -> None:
    first = _cell_observation("one", "overlap")
    second = _cell_observation("overlap", "two")
    outcome, moves, _ = _run_cell_acquire(
        monkeypatch, tmp_path, first, [first, second, second, second],
    )

    assert outcome.is_completed
    assert outcome.outputs == {"records": [
        {"value": "one"},
        {"value": "overlap"},
        {"value": "two"},
    ]}
    assert moves == ["scroll_forward"] * 4


def test_cell_acquire_does_not_confuse_position_only_scroll_with_end(
    monkeypatch, tmp_path,
) -> None:
    first = _cell_observation("one", "two")
    moved = _cell_observation("one", "two", y_offset=-50)
    outcome, _, _ = _run_cell_acquire(
        monkeypatch, tmp_path, first, [moved, moved, moved],
    )

    assert outcome.is_completed
    assert outcome.outputs["records"] == [{"value": "one"}, {"value": "two"}]


def test_cell_acquire_rebinds_one_drifting_adapter_region(
    monkeypatch, tmp_path,
) -> None:
    first = _cell_observation("one", "overlap")
    second = _cell_observation(
        "overlap", "two", fingerprint="android-collection:feed-v2"
    )
    outcome, _, _ = _run_cell_acquire(
        monkeypatch, tmp_path, first, [second, second, second],
    )

    assert outcome.is_completed
    assert outcome.outputs["records"] == [
        {"value": "one"},
        {"value": "overlap"},
        {"value": "two"},
    ]


def test_cell_acquire_reobserves_one_missing_structural_frame(
    monkeypatch, tmp_path,
) -> None:
    first = _cell_observation("one", "overlap")
    missing = Observation(png_bytes=b"png", source="android")
    second = _cell_observation("overlap", "two")
    outcome, _, observed_paths = _run_cell_acquire(
        monkeypatch, tmp_path, first, [missing, second, second, second],
    )

    assert outcome.is_completed
    assert outcome.outputs["records"] == [
        {"value": "one"},
        {"value": "overlap"},
        {"value": "two"},
    ]
    assert observed_paths[:2] == [
        "screenshot_acquire_cells_2_1.png",
        "screenshot_acquire_cells_2_1_retry.png",
    ]


def test_structured_acquire_pages_without_policy_calls(tmp_path):
    observations = [_observation(2, has_next=False, total=2)]
    moves = []

    def make_perception(_platform, _path):
        return _Perception(observations.pop(0))

    bundle = SimpleNamespace(
        make_perception=make_perception,
        move_collection=lambda _platform, _table, family: moves.append(family) or True,
        make_action_policy=lambda _name: (_ for _ in ()).throw(
            AssertionError("structured acquisition must not call a policy")
        ),
        validate_collection_action=None,
        default_action_policy="unused",
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    cursor = ObservationCursor(
        bundle=bundle,
        platform=object(),
        log_dir=tmp_path,
        observation=_observation(1, has_next=True, total=2),
        observation_url="page1.png",
    )
    outcome = execute_acquire_statement(
        _invocation(),
        cursor=cursor,
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )

    assert outcome.is_completed
    assert outcome.verification == "confirmed"
    assert outcome.outputs["records"] == [
        {"ID": "1", "Value": "v1"},
        {"ID": "2", "Value": "v2"},
    ]
    assert moves == ["paginate_next"]
    assert len(context.journal.collection_slices) == 2
    assert {event.strategy for event in context.journal.collection_slices} == {"structured"}
    replayed = EventJournal.model_validate(context.journal.model_dump(mode="json"))
    assert build_acquire_memory(
        replayed, instance_id="i1:collect", statement_id="collect",
    ) == build_acquire_memory(
        context.journal, instance_id="i1:collect", statement_id="collect",
    )


def test_acquire_consumes_exact_lookup_scope_in_multi_collection_frame(tmp_path):
    observation = _observation(1, has_next=False, total=1)
    observation.tables.append({
        "path": "#other",
        "caption": "Other Records",
        "headers": ["ID", "Value"],
        "rows": [{"ID": "other", "Value": "wrong"}],
    })
    invocation = _invocation().model_copy(
        update={"args": {"lookup_scope": _lookup_scope(["ID", "Value"])}},
    )
    invocation.statement.required_fields = ["ID", "Value"]
    bundle = SimpleNamespace(
        make_perception=lambda _platform, _path: _Perception(observation),
        move_collection=lambda *_args: False,
        make_action_policy=lambda _name: (_ for _ in ()).throw(
            AssertionError("complete scoped collection must not call a policy")
        ),
        validate_collection_action=None,
        default_action_policy="unused",
    )
    outcome = _execute(invocation, tmp_path, bundle, observation)

    assert outcome.is_completed
    assert outcome.outputs["records"] == [{"ID": "1", "Value": "v1"}]
    invalid = _invocation().model_copy(
        update={"args": {"lookup_scope": _lookup_scope(["ID"])}},
    )
    invalid.statement.required_fields = ["ID", "Missing"]
    empty_bundle = SimpleNamespace()
    rejected = _execute(invalid, tmp_path, empty_bundle)
    assert rejected.phase == "failed"
    assert "Missing" in rejected.summary


def test_structured_acquire_rewinds_persisted_pager_before_forward_traversal(tmp_path):
    page_one = _observation(1, has_next=True, total=38)
    page_one.tables[0]["rows"] = [
        {"ID": str(index), "Value": f"v{index}"} for index in range(1, 21)
    ]
    page_two = _observation(2, has_next=False, total=38)
    page_two.tables[0]["rows"] = [
        {"ID": str(index), "Value": f"v{index}"} for index in range(21, 39)
    ]
    observations = [page_one, page_two]
    moves = []

    def make_perception(_platform, _path):
        return _Perception(observations.pop(0))

    bundle = SimpleNamespace(
        make_perception=make_perception,
        move_collection=lambda _platform, _table, family: moves.append(family) or True,
        make_action_policy=lambda _name: (_ for _ in ()).throw(
            AssertionError("structured acquisition must not call a policy")
        ),
        validate_collection_action=None,
        default_action_policy="unused",
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    outcome = execute_acquire_statement(
        _invocation(),
        cursor=ObservationCursor(
            bundle=bundle,
            platform=object(),
            log_dir=tmp_path,
            observation=page_two,
            observation_url="page2-initial.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )

    assert outcome.is_completed
    assert outcome.verification == "confirmed"
    assert [record["ID"] for record in outcome.outputs["records"]] == [
        *[str(index) for index in range(21, 39)],
        *[str(index) for index in range(1, 21)],
    ]
    assert moves == ["paginate_prev", "paginate_next"]
    assert len(context.journal.collection_slices) == 3


def test_structured_empty_collection_is_confirmed_without_policy(tmp_path):
    observation = _observation(1, has_next=False, total=0)
    observation.tables[0]["rows"] = []
    bundle = SimpleNamespace(
        make_perception=None,
        move_collection=None,
        make_action_policy=lambda _name: (_ for _ in ()).throw(
            AssertionError("known empty collection must not call a policy")
        ),
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    outcome = execute_acquire_statement(
        _invocation(),
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="empty.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )

    assert outcome.is_completed
    assert outcome.verification == "confirmed"
    assert outcome.outputs == {"records": []}
    assert len(context.journal.collection_slices) == 1


def test_acquire_rejects_ambiguous_structured_collections(tmp_path):
    observation = _observation(1, has_next=False, total=1)
    observation.tables.append({**observation.tables[0], "path": "#other"})
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    bundle = SimpleNamespace(make_perception=None, move_collection=None)
    outcome = execute_acquire_statement(
        _invocation(),
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="frame.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )
    assert outcome.phase == "failed"
    assert "多个" in outcome.summary
    assert not context.journal.collection_slices


def test_react_bound_static_table_completes_without_traversal_guess(
    monkeypatch, tmp_path,
):
    observation = _observation(1, traversal=False)
    observation.tables[0]["traversal"] = {"type": "static"}
    decisions = iter([
        AcquireDecision(
            kind="move",
            reason="bind the requested static table",
            bound_hint="table:0",
            action_family="bind_region",
            target_role="bound_region",
        ),
    ])
    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.decide_acquisition",
        lambda *_args, **_kwargs: next(decisions),
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    bundle = SimpleNamespace(make_perception=None, move_collection=None)

    outcome = execute_acquire_statement(
        _invocation(),
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="frame.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )

    assert outcome.is_completed
    assert outcome.verification == "confirmed"
    assert outcome.outputs["records"] == [{"ID": "1", "Value": "v1"}]
    assert len(context.journal.acquisition_receipts) == 1


def test_react_fallback_requires_two_same_collection_no_progress_moves(monkeypatch, tmp_path):
    decisions = iter(
        [
            AcquireDecision(
                kind="move", reason="bind visible list", bound_hint="table:0",
                action_family="bind_region", target_role="bound_region",
            ),
            AcquireDecision(
                kind="move", reason="move the same list", action_family="scroll_forward",
                target_role="scroll_affordance", instruction="scroll inside Records",
            ),
            AcquireDecision(
                kind="move", reason="confirm the same boundary", action_family="scroll_forward",
                target_role="scroll_affordance", instruction="scroll inside Records again",
            ),
            AcquireDecision(kind="boundary", reason="visible end and two moves made no progress"),
        ]
    )
    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.decide_acquisition",
        lambda *_args, **_kwargs: next(decisions),
    )
    observation = _observation(1, traversal=False)
    bundle = SimpleNamespace(
        make_perception=lambda _platform, _path: _Perception(observation),
        move_collection=None,
        validate_collection_action=lambda *_args: True,
        default_action_policy="visual",
        make_action_policy=lambda _name: SimpleNamespace(
            decide=lambda *_args, **_kwargs: BaseActionDecision(
                action=BaseAction(
                    action_type="scroll", x=500, y=500, direction="down",
                    description="scroll bound collection",
                )
            )
        ),
        make_executor=lambda _platform: SimpleNamespace(execute=lambda *_args, **_kwargs: True),
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    outcome = execute_acquire_statement(
        _invocation(),
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="frame.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
        max_moves=5,
    )
    assert outcome.is_completed
    assert outcome.verification == "confirmed"
    assert len(context.journal.collection_slices) == 3
    assert len(outcome.outputs["records"]) == 3
    memory = build_acquire_memory(
        context.journal, instance_id="i1:collect", statement_id="collect",
    )
    assert not hasattr(memory, "phase")
    assert [event.status for event in memory.receipts if event.action_family == "scroll_forward"] == [
        "observed", "observed",
    ]


def test_complete_budget_exhaustion_never_claims_partial_success(monkeypatch, tmp_path):
    decisions = iter([
        AcquireDecision(
            kind="move", reason="bind visible list", bound_hint="table:0",
            action_family="bind_region", target_role="bound_region",
        ),
        AcquireDecision(
            kind="move", reason="keep waiting", action_family="wait",
            target_role="scroll_affordance", instruction="wait for more records",
        ),
        AcquireDecision(kind="boundary", reason="one wait did not expose more records"),
    ])
    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.decide_acquisition",
        lambda *_args, **_kwargs: next(decisions),
    )
    observation = _observation(1, traversal=False)
    bundle = SimpleNamespace(
        make_perception=lambda _platform, _path: _Perception(observation),
        move_collection=None,
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    cursor = ObservationCursor(
        bundle=bundle, platform=object(), log_dir=tmp_path,
        observation=observation, observation_url="frame.png",
    )
    outcome = execute_acquire_statement(
        _invocation("complete"),
        cursor=cursor,
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
        max_moves=1,
    )
    assert outcome.phase == "exhausted"

    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.decide_acquisition",
        lambda *_args, **_kwargs: AcquireDecision(
            kind="move", reason="try another scroll", action_family="scroll_forward",
            target_role="scroll_affordance", instruction="scroll bound collection",
        ),
    )
    bundle.make_action_policy = lambda _name: (_ for _ in ()).throw(
        AssertionError("replay must not reset the acquisition budget")
    )
    replayed = execute_acquire_statement(
        _invocation("complete"),
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="replay.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
        max_moves=1,
    )
    assert replayed.phase == "exhausted"


def test_react_fallback_rejects_business_action_family_before_dispatch(monkeypatch, tmp_path):
    decisions = iter([
        AcquireDecision(
            kind="move", reason="bind visible list", bound_hint="table:0",
            action_family="bind_region", target_role="bound_region",
        ),
        AcquireDecision(
            kind="move", reason="scroll only", action_family="scroll_forward",
            target_role="scroll_affordance", instruction="scroll the bound list",
        ),
        AcquireDecision(kind="blocked", reason="the allowed move was rejected"),
    ])
    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.decide_acquisition",
        lambda *_args, **_kwargs: next(decisions),
    )
    observation = _observation(1, traversal=False)
    dispatched = []
    bundle = SimpleNamespace(
        make_perception=lambda _platform, _path: _Perception(observation),
        move_collection=None,
        validate_collection_action=lambda *_args: True,
        default_action_policy="visual",
        make_action_policy=lambda _name: SimpleNamespace(
            decide=lambda *_args, **_kwargs: BaseActionDecision(
                action=BaseAction(
                    action_type="tap", x=500, y=500,
                    description="open a business row",
                )
            )
        ),
        make_executor=lambda _platform: SimpleNamespace(
            execute=lambda *_args, **_kwargs: dispatched.append(True) or True,
        ),
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    outcome = execute_acquire_statement(
        _invocation(),
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="frame.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
    )
    assert outcome.phase == "failed"
    assert dispatched == []
    assert "rejected" in {
        event.status for event in context.journal.acquisition_receipts
    }


def test_browser_affordance_guard_reads_callable_viewport_size():
    class Client:
        def viewport_size(self):
            return (1000, 800)

        def _cdp_send(self, _method, _params):
            return {"result": {"value": True}}

    decision = BaseActionDecision(
        action=BaseAction(action_type="tap", x=500, y=500, description="next page")
    )
    assert validate_collection_action(
        SimpleNamespace(client=Client()), {"path": "#records"}, decision, "paginate_next"
    ) is True


def test_mobile_visual_acquire_is_journal_replayable_without_private_phase(monkeypatch, tmp_path):
    decisions = iter([
        AcquireDecision(
            kind="move", reason="bind the visible list", bound_hint="visual:main",
            action_family="bind_region", target_role="bound_region",
        ),
        AcquireDecision(
            kind="move", reason="move the same visible list", action_family="scroll_forward",
            target_role="scroll_affordance", instruction="scroll the visible list forward",
        ),
        AcquireDecision(
            kind="move", reason="confirm its end", action_family="scroll_forward",
            target_role="scroll_affordance", instruction="scroll the same list forward again",
        ),
        AcquireDecision(kind="boundary", reason="visible end with repeated no progress"),
    ])
    monkeypatch.setattr(
        "gui_agent.core.run.statements.acquire.decide_acquisition",
        lambda *_args, **_kwargs: next(decisions),
    )
    vision_calls = []
    monkeypatch.setattr(
        "gui_agent.core.run.structured_read.structured_read_rows",
        lambda *_args, **_kwargs: vision_calls.append(1) or [{"name": "A"}],
    )
    observation = Observation(png_bytes=b"png", source="android")
    bundle = SimpleNamespace(
        prepare_vision_prompt_png=lambda value: value,
        make_perception=lambda _platform, _path: _Perception(observation),
        move_collection=None,
        validate_collection_action=None,
        default_action_policy="visual",
        make_action_policy=lambda _name: SimpleNamespace(
            decide=lambda *_args, **_kwargs: BaseActionDecision(
                action=BaseAction(
                    action_type="scroll", x=500, y=500, direction="down",
                    description="scroll the bound collection",
                )
            )
        ),
        make_executor=lambda _platform: SimpleNamespace(execute=lambda *_args, **_kwargs: True),
    )
    context = PolicyContext(goal="collect", supervisor_policy_name="s", action_policy_name="a")
    invocation = _invocation()
    invocation.statement.required_fields = ["name"]
    outcome = execute_acquire_statement(
        invocation,
        cursor=ObservationCursor(
            bundle=bundle, platform=object(), log_dir=tmp_path,
            observation=observation, observation_url="frame.png",
        ),
        bundle=bundle,
        platform=object(),
        context=context,
        instance_id="i1:collect",
        save_context=lambda: None,
        say=lambda _message: None,
        status=lambda _message: None,
        max_moves=5,
    )

    assert outcome.is_completed
    assert len(vision_calls) == 3  # initial frame plus the two physically refreshed windows
    assert len(context.journal.collection_slices) == 3
    replayed = EventJournal.model_validate(context.journal.model_dump(mode="json"))
    memory = build_acquire_memory(
        replayed, instance_id="i1:collect", statement_id="collect",
    )
    assert memory.bound_region.startswith("visual:android:")
    assert "phase" not in memory.__dataclass_fields__
