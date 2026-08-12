from __future__ import annotations

import json

from gui_agent.core.tool_agent.contracts import CollectionRef, WorkerOutcome, WorkerSpec
from gui_agent.core.tool_agent.replay import (
    RecordedContext,
    RecordedGuiWorker,
    load_recorded_run,
    replay_program,
    replay_recorded_run,
)


ROW_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}


def _worker_spec() -> WorkerSpec:
    return WorkerSpec(
        profile="collector",
        goal="Collect normalized records",
        success_criteria=["All records are collected"],
        data_requirements=[{
            "id": "records",
            "description": "Normalized records",
            "row_schema": ROW_SCHEMA,
        }],
        actions=[{
            "name": "reveal_more",
            "capability": "scroll",
            "description": "Reveal additional records",
            "fixed_args": {"direction": "down"},
            "exposed_args": ["amount"],
        }],
        max_steps=4,
    )


def _source(spec: WorkerSpec) -> str:
    payload = spec.model_dump(mode="json")
    return f'''def run(ctx):
    collected = ctx.gui_worker(
        worker_id="collect_records",
        profile={payload["profile"]!r},
        goal={payload["goal"]!r},
        success_criteria={payload["success_criteria"]!r},
        data_requirements={payload["data_requirements"]!r},
        actions={payload["actions"]!r},
        max_steps={payload["max_steps"]!r},
    )
    computed = ctx.transform(
        transform_id="sum_records",
        inputs=[collected["collection_ref"]["ref"]],
        source="def transform(inputs):\\n    return sum(row['value'] for row in inputs[0])",
        result_schema={{"type": "integer"}},
    )
    ctx.finish(computed["ref"], effect="data")
'''


def _collection() -> CollectionRef:
    return CollectionRef(
        ref="collection:records",
        requirement_id="records",
        chunk_refs=["chunk:records:1"],
        row_count=2,
        row_schema=ROW_SCHEMA,
        coverage={"scope_status": "met", "status": "complete"},
    )


def _recording(spec: WorkerSpec) -> RecordedContext:
    return RecordedContext(
        gui_workers={
            "collect_records": (RecordedGuiWorker(
                worker_id="collect_records",
                spec=spec,
                outcome=WorkerOutcome(
                    phase="completed",
                    summary="Collected two records",
                    collection_ref=_collection(),
                    steps=3,
                ),
                value=[{"value": 2}, {"value": 5}],
            ),),
        },
        expected_phase="completed",
        expected_output=7,
    )


def test_replay_restores_gui_collection_but_reexecutes_transform(monkeypatch) -> None:
    spec = _worker_spec()
    calls = []

    def execute(source, values, schema):
        calls.append((source, values, schema))
        return sum(row["value"] for row in values[0])

    monkeypatch.setattr(
        "gui_agent.core.tool_agent.orchestrator.execute_transform",
        execute,
    )

    result = replay_program(_source(spec), _recording(spec))

    assert result.ok, result.error
    assert result.output == 7
    assert len(calls) == 1
    assert calls[0][1] == [[{"value": 2}, {"value": 5}]]
    assert any(event["event"] == "transform_completed" for event in result.trace)


def test_replay_rejects_a_changed_gui_worker_contract() -> None:
    spec = _worker_spec()
    changed = _source(spec).replace("max_steps=4", "max_steps=5")

    result = replay_program(changed, _recording(spec))

    assert result.status == "failed"
    assert "specification does not match" in result.error


def test_load_recorded_run_replays_normal_runtime_artifacts(tmp_path, monkeypatch) -> None:
    spec = _worker_spec()
    source = _source(spec)
    outcome = _recording(spec).gui_workers["collect_records"][0].outcome
    (tmp_path / "tool_agent_trace.json").write_text(json.dumps({
        "phase": "completed",
        "output": 7,
        "platform_time": {
            "platform": "browser",
            "local_datetime": "2026-08-12T19:10:27+08:00",
            "timezone": "Asia/Shanghai",
            "utc_offset": "+08:00",
            "source": "browser_cdp",
            "confidence": "authoritative",
            "captured_at": "2026-08-12T11:10:27.000+00:00",
            "fallback_reason": "",
        },
        "trace": [
            {
                "event": "master_program_generated",
                "source": source,
            },
            {
                "event": "master_program_execution_started",
                "execution": 1,
                "source": source,
            },
            {
                "event": "master_worker_dispatch",
                "worker_id": "collect_records",
                "kind": "gui",
                "spec": spec.model_dump(mode="json"),
            },
            {
                "event": "master_worker_result",
                "worker_id": "collect_records",
                "kind": "gui",
                "outcome": outcome.model_dump(mode="json"),
            },
            {
                "event": "master_program_completed",
                "execution": 1,
                "phase": "completed",
                "result_ref": "result:1",
            },
        ],
    }), encoding="utf-8")
    (tmp_path / "tool_agent_data_store.json").write_text(json.dumps({
        "values": {"collection:records": [{"value": 2}, {"value": 5}]},
    }), encoding="utf-8")
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.orchestrator.execute_transform",
        lambda source, values, schema: 7,
    )

    recorded = load_recorded_run(tmp_path)
    result = replay_recorded_run(recorded)

    assert len(recorded.programs) == 1
    assert result.ok, result.error
    assert result.output == 7
    assert result.platform_time["source"] == "browser_cdp"
