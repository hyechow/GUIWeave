"""Promote selected Worker frames from a run directory into a compact replay fixture."""

from __future__ import annotations

import argparse
import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

_STABLE_ARGS = (
    "app", "text", "url", "direction", "amount", "target_area", "option", "key",
)


def _normalize_paths(value: Any, source: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(source), ".")
    if isinstance(value, list):
        return [_normalize_paths(item, source) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_paths(item, source)
            for key, item in value.items()
        }
    return value


def _compact_event(event: dict[str, Any], source: Path) -> dict[str, Any]:
    kept = {
        key: deepcopy(event[key])
        for key in (
            "index", "event", "worker_id", "frame_id", "step", "tool", "args",
            "state", "replay_context",
        )
        if key in event
    }
    kept["context_reports"] = [
        report for report in event.get("context_reports", [])
        if report.get("kind") == "prompt_snapshot"
        and report.get("label") == "tool_agent.worker"
    ]
    return _normalize_paths(kept, source)


def _default_case(event: dict[str, Any]) -> dict[str, Any]:
    calls = event.get("args", {}).get("actions") or []
    actions = {
        item["name"]: item
        for item in event.get("replay_context", {}).get("actions", [])
    }
    required = []
    for call in calls:
        name, args = str(call.get("name") or ""), call.get("args") or {}
        capability = str(actions.get(name, {}).get("capability") or name)
        required.append({
            "capability": capability,
            **(
                {"args": {
                    key: args[key]
                    for key in _STABLE_ARGS
                    if args.get(key) not in (None, "")
                }}
                if any(args.get(key) not in (None, "") for key in _STABLE_ARGS)
                else {}
            ),
        })
    frame = int(str(event["frame_id"]).removeprefix("frame:"))
    return {
        "id": f"frame_{frame}", "frame": frame, "min_pass_rate": 1.0,
        "expected": {
            "tool": event.get("tool"), "protocol_repairs_max": 0,
            "required_prefix": required,
        },
    }


def promote_decisions(
    source: Path,
    destination: Path,
    frames: list[int],
) -> dict[str, Any]:
    source, destination = source.resolve(), destination.resolve()
    manifest_path = destination / "manifest.json"
    existing_cases = {}
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing_cases = {
            int(case["frame"]): case
            for case in existing.get("cases", [])
            if isinstance(case, dict) and "frame" in case
        }
    trace = json.loads((source / "tool_agent_trace.json").read_text(encoding="utf-8"))
    selected = [
        event for event in trace.get("trace", [])
        if event.get("event") == "worker_decision"
        and int(str(event.get("frame_id") or "0").removeprefix("frame:")) in frames
    ]
    by_frame = {
        int(str(event["frame_id"]).removeprefix("frame:")): event
        for event in selected
    }
    missing = sorted(set(frames) - set(by_frame))
    if missing:
        raise ValueError(f"run has no Worker decision for frame(s): {missing}")
    destination.mkdir(parents=True, exist_ok=True)
    compact = [
        _compact_event(by_frame[frame], source) for frame in frames
    ]
    for frame in frames:
        screenshot = source / f"screenshot_tool_agent_{frame}.png"
        observation = json.loads(
            (source / f"observation_tool_agent_{frame}.json").read_text(encoding="utf-8")
        )
        observation["screenshot_path"] = screenshot.name
        promoted_screenshot = destination / screenshot.name
        shutil.copy2(screenshot, promoted_screenshot)
        (destination / f"observation_tool_agent_{frame}.json").write_text(
            json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    context = json.loads((source / "context.json").read_text(encoding="utf-8"))
    (destination / "context.json").write_text(
        json.dumps({"platform": context.get("platform", "android")}, indent=2),
        encoding="utf-8",
    )
    (destination / "tool_agent_trace.json").write_text(
        json.dumps({"trace": compact}, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    manifest = {
        "version": 1,
        "name": destination.name,
        "source_run": source.name,
        "cases": [
            existing_cases.get(frame, _default_case(by_frame[frame]))
            for frame in frames
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--frames", type=int, nargs="+", required=True)
    args = parser.parse_args()
    manifest = promote_decisions(args.source, args.destination, args.frames)
    print(f"Promoted {len(manifest['cases'])} decision case(s) to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
