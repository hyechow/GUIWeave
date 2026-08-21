"""Run curated Worker decision cases with recorded or configured model responses."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from replay.decision import replay_worker_decision


def load_decision_manifest(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not isinstance(manifest.get("cases"), list):
        raise ValueError("decision suite manifest must have version=1 and a cases array")
    for case in manifest["cases"]:
        if not all(key in case for key in ("id", "frame", "expected")):
            raise ValueError("each decision case requires id, frame, and expected")
        rate = float(case.get("min_pass_rate", 1.0))
        if not 0 < rate <= 1:
            raise ValueError(f"case {case['id']!r} min_pass_rate must be in (0, 1]")
    return manifest


def _decision_event(run_dir: Path, frame: int) -> dict[str, Any]:
    trace = json.loads((run_dir / "tool_agent_trace.json").read_text(encoding="utf-8"))
    try:
        return next(
            event for event in trace.get("trace", [])
            if event.get("event") == "worker_decision"
            and event.get("frame_id") == f"frame:{frame}"
        )
    except StopIteration as exc:
        raise ValueError(f"fixture has no Worker decision for frame:{frame}") from exc


def _public_args(args: Any) -> dict[str, Any]:
    return {
        key: value for key, value in (args.items() if isinstance(args, dict) else ())
        if not str(key).startswith("_")
    }


def recorded_decision_response(run_dir: Path, frame: int) -> AIMessage:
    event = _decision_event(run_dir, frame)
    tool = str(event.get("tool") or "")
    state = dict(event.get("state") or {})
    # Older promoted fixtures predate typed WorkerMemory. Recorded-mode replay
    # normalizes that retired presentation field without changing the decision.
    established_facts = state.pop("established_facts", None) or []
    state.setdefault("memory_updates", [
        {
            "fact_type": "evidence",
            "key": f"recorded_fact_{index}",
            "status": "active",
            "lifetime": "attempt",
            "statement": str(statement),
            "depends_on": [],
        }
        for index, statement in enumerate(established_facts, start=1)
    ])
    if tool == "continue_with_actions":
        args = {
            "state": state,
            "actions": [
                {"name": call["name"], "args": _public_args(call.get("args"))}
                for call in (event.get("args", {}).get("actions") or [])
                if isinstance(call, dict) and call.get("name")
            ],
        }
    else:
        args = {"state": state, **_public_args(event.get("args"))}
    return AIMessage(content="", tool_calls=[{
        "id": f"recorded-frame-{frame}", "name": tool, "args": args,
    }])


class RecordedDecisionModel:
    """Minimal model transport that replays one normalized recorded response."""

    def __init__(self, response: AIMessage) -> None:
        self.response = response

    def bind(self, **_kwargs: Any) -> "RecordedDecisionModel":
        return self

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> "RecordedDecisionModel":
        return self

    def invoke(self, _messages: Any) -> AIMessage:
        return self.response


def _sample_calls(sample: dict[str, Any]) -> list[dict[str, Any]]:
    if sample.get("tool") == "continue_with_actions":
        calls = sample.get("args", {}).get("actions") or []
        return [call for call in calls if isinstance(call, dict)]
    return [{"name": sample.get("tool"), "args": sample.get("args") or {}}]


def _match_action(
    expected: dict[str, Any],
    semantic: dict[str, Any],
    call: dict[str, Any],
    path: str,
) -> list[str]:
    if alternatives := expected.get("one_of"):
        results = [
            _match_action(alternative, semantic, call, path)
            for alternative in alternatives
        ]
        if any(not result for result in results):
            return []
        capability = str(semantic.get("capability") or call.get("name") or "")
        matching = [
            result for alternative, result in zip(alternatives, results)
            if alternative.get("capability") == capability
        ]
        return min(matching or results, key=len)

    errors = []
    capability = str(semantic.get("capability") or call.get("name") or "")
    if capability != expected.get("capability"):
        errors.append(
            f"{path}.capability: expected {expected.get('capability')!r}, "
            f"got {capability!r}"
        )
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    for key, value in (expected.get("args") or {}).items():
        if args.get(key) != value:
            errors.append(f"{path}.{key}: expected {value!r}, got {args.get(key)!r}")
    for key, bounds in (expected.get("arg_ranges") or {}).items():
        value = args.get(key)
        lower, upper = map(float, bounds)
        if not isinstance(value, (int, float)) or not lower <= float(value) <= upper:
            errors.append(
                f"{path}.{key}: expected in {bounds!r}, got {value!r}"
            )
    if box := expected.get("target_box"):
        x, y = args.get("x"), args.get("y")
        left, top, right, bottom = map(float, box)
        if expected.get("target_match") == "groundable":
            x_tolerance = float(expected.get("crop_half_width", 400.0))
            y_tolerance = float(expected.get("crop_half_height", 180.0))
        else:
            x_tolerance = y_tolerance = float(
                expected.get("target_tolerance", 6.0)
            )
        if not (
            isinstance(x, (int, float)) and isinstance(y, (int, float))
            and left - x_tolerance <= float(x) <= right + x_tolerance
            and top - y_tolerance <= float(y) <= bottom + y_tolerance
        ):
            errors.append(f"{path}.point: {(x, y)!r} is outside {box!r}")
    description = str(args.get("description") or "").casefold()
    for token in expected.get("description_contains") or []:
        if str(token).casefold() not in description:
            errors.append(f"{path}.description: missing {token!r}")
    return errors


def score_decision_sample(sample: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    errors = []
    if sample.get("tool") != expected.get("tool", "continue_with_actions"):
        errors.append(
            f"tool: expected {expected.get('tool')!r}, got {sample.get('tool')!r}"
        )
    repairs = int(sample.get("protocol_repairs") or 0)
    if repairs > int(expected.get("protocol_repairs_max", 0)):
        errors.append(
            "protocol_repairs: expected <= "
            f"{expected.get('protocol_repairs_max', 0)}, got {repairs}"
        )

    semantics = sample.get("action_semantics") or []
    calls = _sample_calls(sample)
    forbidden = set(expected.get("forbidden_capabilities") or [])
    used = {
        str(item.get("capability") or "")
        for item in semantics
        if isinstance(item, dict)
    }
    if blocked := sorted(used & forbidden):
        errors.append(f"forbidden capabilities: {blocked}")

    required = expected.get("required_prefix") or []
    optional = expected.get("optional_suffix") or []
    if not len(required) <= len(calls) <= len(required) + len(optional):
        errors.append(
            f"actions: expected {len(required)}..{len(required) + len(optional)}, got {len(calls)}"
        )
    expected_actions = [
        *required,
        *optional[:max(0, len(calls) - len(required))],
    ]
    for index, wanted in enumerate(expected_actions):
        if index >= len(calls) or index >= len(semantics):
            break
        errors.extend(_match_action(wanted, semantics[index], calls[index], f"actions[{index}]"))

    state = sample.get("args", {}).get("state", {})
    facts = list(state.get("established_facts") or [])
    facts.extend(
        str(item.get("statement") or "")
        for item in (state.get("memory_updates") or [])
        if isinstance(item, dict)
    )
    fact_text = "\n".join(str(item) for item in facts)
    for needle in expected.get("established_facts_contain") or []:
        if str(needle) not in fact_text:
            errors.append(f"established_facts: missing {needle!r}")
    return errors


def replay_decision_suite(
    run_dir: Path,
    *,
    samples: int = 1,
    recorded: bool = False,
    group: str = "all",
) -> dict[str, Any]:
    manifest = load_decision_manifest(run_dir)
    cases = [
        case for case in manifest["cases"]
        if group == "all" or case.get("group") == group
    ]
    if not cases:
        raise ValueError(f"decision suite has no cases in group {group!r}")
    results = []
    for case in cases:
        frame = int(case["frame"])
        model = (
            RecordedDecisionModel(recorded_decision_response(run_dir, frame))
            if recorded else None
        )
        try:
            replay = replay_worker_decision(
                run_dir, frame=frame, samples=samples, expectation={}, llm=model,
            )
            scored = []
            for sample in replay["samples"]:
                errors = score_decision_sample(sample, case["expected"])
                scored.append({**sample, "passed": not errors, "errors": errors})
            passed = sum(bool(sample["passed"]) for sample in scored)
            required = math.ceil(
                samples * float(case.get("min_pass_rate", 1.0)) - 1e-6
            )
            ok = passed >= required
            results.append({
                "id": case["id"], "frame": frame, "passed": ok,
                "passed_samples": passed, "required_samples": required,
                "model": replay.get("model"), "samples": scored,
            })
        except Exception as exc:  # noqa: BLE001 - one case must not hide the suite report
            results.append({
                "id": case.get("id", f"frame_{frame}"), "frame": frame,
                "passed": False, "passed_samples": 0, "required_samples": samples,
                "error": f"{type(exc).__name__}: {exc}", "samples": [],
            })
    passed_cases = sum(bool(case["passed"]) for case in results)
    return {
        "status": "passed" if passed_cases == len(results) else "failed",
        "suite": manifest.get("name") or run_dir.name,
        "group": group,
        "mode": "recorded" if recorded else "model",
        "summary": f"Decision suite: {passed_cases}/{len(results)} case(s) passed.",
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--recorded", action="store_true")
    parser.add_argument("--group", default="all")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        parser.error("--samples must be between 1 and 10")
    result = replay_decision_suite(
        args.run_dir.expanduser().resolve(), samples=args.samples,
        recorded=args.recorded, group=args.group,
    )
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[{result['status'].upper()}] {result['summary']}")
        for case in result["cases"]:
            print(
                f"case={case['id']} passed={case['passed']} "
                f"samples={case['passed_samples']}/{case['required_samples']}"
            )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
