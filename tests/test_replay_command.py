import json
from types import SimpleNamespace

from gui_agent.core.orchestrator import Acquire, Finish, Interact, OutputSpec, Program
from gui_agent.core.run.context import save_context, save_observation_snapshot
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.replay import main, replay_log
from gui_agent.core.schemas import (
    CollectionProvenance,
    CollectionSliceEvent,
    Observation,
    PolicyContext,
    StatementOutcome,
    StatementOutcomeEvent,
)
from replay.run import _statement_for_terminal_observation, _statement_for_turn


def _checkpoint(tmp_path, *, with_snapshot=True):
    program = Program(statements=[
        Acquire(
            id="collect",
            bind="observed",
            goal="materialize the scoped collection",
            returns={
                "rows": OutputSpec(type="list[record]", coverage="complete"),
            },
        ),
        Finish(message="done"),
    ])
    context = PolicyContext(
        goal="collect",
        supervisor_policy_name="statement",
        action_policy_name="vision",
    )
    runtime = ProgramRuntime.start(program, journal=context.journal)
    instance_id = runtime.next_instance_id("collect")
    context.journal.append_collection_slice(CollectionSliceEvent(
        event_ref="collection:1",
        statement_instance_id=instance_id,
        statement_id="collect",
        frame_ref="screenshot_turn_1.png",
        collection_key="records",
        provenance=CollectionProvenance(
            surface_fingerprint="#records",
            schema_fingerprint="schema",
            route="/records",
        ),
        window_key="page:1",
        content_key="content:1",
        records=[{"id": "1"}],
        known_total=1,
        boundary="at_end",
    ))
    outcome = StatementOutcome.completed(
        "collected",
        outputs={"rows": [{"id": "1"}]},
        evidence=["collection:1"],
    )
    context.journal.append_statement_outcome(StatementOutcomeEvent(
        after_turn=0,
        statement_instance_id=instance_id,
        statement_id="collect",
        outcome=outcome,
    ))
    save_context(tmp_path / "context.json", context)
    if with_snapshot:
        (tmp_path / "screenshot_turn_1.png").write_bytes(b"png")
        save_observation_snapshot(
            tmp_path / "observation_turn_1.json",
            Observation(png_bytes=b"png", source="browser", url="https://example.test"),
            screenshot="screenshot_turn_1.png",
        )


def test_replay_log_rebuilds_runtime_acquire_and_observation(tmp_path):
    _checkpoint(tmp_path)
    summary = replay_log(tmp_path)

    assert summary["offline"] is True
    assert summary["runtime"]["finished"] is True
    assert summary["runtime"]["env"] == {"observed": {"rows": [{"id": "1"}]}}
    assert summary["observations"]["snapshots"] == 1
    assert summary["collections"] == [{
        "instance_id": "i1:collect",
        "statement_id": "collect",
        "collection_key": "records",
        "records": 1,
        "segments": 1,
        "coverage": "complete",
        "may_contain_duplicates": False,
        "provenance_drift": False,
        "bound_region": "",
        "attempts": 0,
        "failed_capabilities": [],
    }]


def test_replay_cli_emits_json_and_fails_on_broken_snapshot(tmp_path, capsys):
    _checkpoint(tmp_path)
    assert main([str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    (tmp_path / "screenshot_turn_1.png").unlink()
    assert main([str(tmp_path), "--json"]) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["valid"] is False
    assert "replay screenshot not found" in failure["error"]


def test_turn_replay_reads_current_statement_info_and_program_ids():
    statement = {
        "id": "show-field",
        "executor": "interact",
        "goal": "show the required field",
        "success": "the field is visible",
        "required_values": {"field": "priority"},
        "observe_fields": ["priority"],
    }
    contract = _statement_for_turn({}, SimpleNamespace(
        index=16,
        supervisor=SimpleNamespace(statement_id="show-field"),
        statement=statement,
        statement_instance_id="i1:show-field",
    ))
    assert contract.required_values == {"field": "priority"}
    assert contract.observe_fields == ["priority"]

    program = Program(statements=[Interact(
        id="show-field",
        goal="show the required field",
        success="the field is visible",
        observe_fields=["priority"],
    )])
    terminal = _statement_for_terminal_observation(
        {"orchestrator": {"program": program.model_dump(mode="json")}},
        statement_id="show-field",
    )
    assert terminal.id == "show-field"
    assert terminal.observe_fields == ["priority"]
