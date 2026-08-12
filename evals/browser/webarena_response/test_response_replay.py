"""Replay promoted WebArena response-synthesis inputs without consulting evaluator truth."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.adapters.browser.webarena import _synthesize_response
from gui_agent.core.run.result import AgentResult


REPLAYS = Path(__file__).parent / "replays"


def test_completed_mutate_replays() -> None:
    for path in sorted(REPLAYS.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        raw = case["result"]
        response = _synthesize_response(
            case["intent"],
            AgentResult(
                goal=case["intent"],
                output=str(raw.get("output") or ""),
                summary=str(raw.get("result_summary") or raw.get("stop_reason") or ""),
                phase=raw["phase"],
                verification=raw.get("verification"),
                task_type=raw.get("task_type"),
                orchestrator={"kind": "tool_agent", "effect": "mutation"},
            ),
        )
        assert response.model_dump(mode="json") == case["expected"], path.name
