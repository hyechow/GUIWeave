from __future__ import annotations

import json
import subprocess
from pathlib import Path

from replay.suite import _case_command, run_suite


def _manifest(tmp_path: Path) -> Path:
    for run_dir in ("reach", "commit"):
        (tmp_path / run_dir).mkdir()
    path = tmp_path / "suite.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "name": "sample",
            "cases": [
                {
                    "name": "reach",
                    "run_dir": str(tmp_path / "reach"),
                    "turn": 2,
                    "expectation": {"should_act": False},
                },
                {
                    "name": "commit",
                    "run_dir": str(tmp_path / "commit"),
                    "turn": 7,
                    "statement_id": "c3",
                    "with_action_policy": True,
                    "expectation": {"atomic_role": "commit"},
                },
            ],
        }),
        encoding="utf-8",
    )
    return path


def test_replay_suite_runs_every_case_and_aggregates_failures(tmp_path: Path) -> None:
    suite_path = _manifest(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        expectation = json.loads(command[command.index("--expect-json") + 1])
        calls.append((command, expectation))
        return subprocess.CompletedProcess(
            command,
            0 if len(calls) == 1 else 1,
            stdout="replay output",
            stderr="",
        )

    result = run_suite(suite_path, process_runner=fake_run)

    assert result["passed"] is False
    assert result["passed_count"] == 1
    assert result["case_count"] == 2
    assert [expectation for _, expectation in calls] == [
        {"should_act": False},
        {"atomic_role": "commit"},
    ]
    assert "--statement-id" in calls[1][0]
    assert "--with-action-policy" in calls[1][0]


def test_replay_suite_routes_read_cases_to_single_frame_runner() -> None:
    request = {"fields": {"temperature": "number"}}
    command = _case_command({
        "executor": "read",
        "run_dir": "fixture",
        "turn": 3,
        "request": request,
        "expectation": {"outputs": {"temperature": 34}},
    })

    assert command[1:3] == ["-m", "replay.read"]
    assert json.loads(
        command[command.index("--request-json") + 1]
    ) == request
