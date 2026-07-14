"""Replay promoted WebArena response-synthesis inputs without consulting evaluator truth."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.adapters.browser.webarena import _synthesize_response


REPLAYS = Path(__file__).parent / "replays"


def test_completed_mutate_replays() -> None:
    for path in sorted(REPLAYS.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        response = _synthesize_response(case["intent"], case["result"])
        assert response.model_dump(mode="json") == case["expected"], path.name
