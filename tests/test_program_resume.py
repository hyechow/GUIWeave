from __future__ import annotations

import json

from gui_agent.adapters.browser.webarena import (
    _merge_har_segment,
    _resume_inputs,
    _stage_resume_assets,
)
from gui_agent.core.orchestrator import CodingProgram, CodingProgramRuntime
from gui_agent.core.schemas import (
    EventJournal,
    StatementOutcome,
    StatementOutcomeEvent,
)


def _event(
    statement_id: str,
    instance_id: str,
    outcome: StatementOutcome,
) -> StatementOutcomeEvent:
    return StatementOutcomeEvent(
        statement_instance_id=instance_id,
        statement_id=statement_id,
        outcome=outcome,
    )


def test_runtime_resume_rebuilds_locals_and_ignores_failed_attempt(request) -> None:
    program = CodingProgram(
        goal="update one order",
        source="""
def run(ctx):
    state = ctx.reach("Open Orders", success={"entity": "Orders"})
    rows = ctx.query(state, entity="Orders", fields=["ID"])
    if rows:
        ctx.commit(goal="Update order", target=rows[0], values={"Status": "Complete"})
    return len(rows)
""",
    )
    outcomes = [
        _event("c1", "i1:c1", StatementOutcome.completed("orders open")),
        _event(
            "c2",
            "i2:c2",
            StatementOutcome.completed(
                "scope",
                outputs={
                    "scope": {
                        "kind": "resolved_collection",
                        "entity": "Orders",
                        "surface_fingerprint": "table:#orders",
                        "available_fields": ["ID"],
                    }
                },
            ),
        ),
        _event(
            "c3",
            "i3:c3",
            StatementOutcome.completed(
                "rows",
                outputs={"rows": [{"ID": "42"}]},
            ),
        ),
        _event("c4", "i4:c4", StatementOutcome.failed("turn budget")),
    ]

    runtime = CodingProgramRuntime.resume(
        program,
        EventJournal(events=outcomes),
    )
    request.addfinalizer(runtime.close)

    assert runtime.current is not None
    assert runtime.current.id == "c4"
    assert runtime.current.inputs["target"]["ID"] == "42"
    assert len(runtime.interpreter.run_log) == 3
    assert runtime.next_instance_id("c4") == "i5:c4"


def test_resume_inputs_and_har_merge(tmp_path) -> None:
    log_dir = tmp_path / "run"
    log_dir.mkdir()
    output_dir = tmp_path / "output"
    context = {
        "goal": "resume me",
        "orchestrator": {
            "program": CodingProgram(
                goal="resume me",
                source="def run(ctx):\n    return 1\n",
            ).model_dump(mode="json")
        },
        "webarena": {
            "task_id": 9,
            "sites": ["shopping_admin"],
            "intent": "resume me",
            "start_url": "http://example.test/admin",
            "task_output_dir": str(output_dir),
        },
    }
    (log_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")

    task, restored_output_dir, source_har, program = _resume_inputs(log_dir)

    assert task["task_id"] == 9
    assert restored_output_dir == output_dir
    assert source_har == output_dir / "network.har"
    assert program.source == "def run(ctx):\n    return 1\n"

    base = output_dir / "network.har"
    base.parent.mkdir()
    base.write_text(
        json.dumps({"log": {"version": "1.2", "entries": [{"request": {"url": "a"}}]}}),
        encoding="utf-8",
    )
    segment = log_dir / "network_resume.har"
    segment.write_text(
        json.dumps({"log": {"version": "1.2", "entries": [{"request": {"url": "b"}}]}}),
        encoding="utf-8",
    )

    assert _merge_har_segment(base, segment) == (1, 1)
    entries = json.loads(base.read_text(encoding="utf-8"))["log"]["entries"]
    assert [entry["request"]["url"] for entry in entries] == ["a", "b"]


def test_stage_resume_assets_copies_history_without_touching_source(tmp_path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    original = source / "screenshot_turn_29.png"
    original.write_bytes(b"checkpoint")
    (source / "context.json").write_text("{}", encoding="utf-8")

    _stage_resume_assets(source, target)
    (target / "screenshot_turn_29.png").write_bytes(b"derived")

    assert original.read_bytes() == b"checkpoint"
    assert not (target / "context.json").exists()
