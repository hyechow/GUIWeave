"""Replay one single-frame Read without executing GUI actions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator.models import field_projection
from gui_agent.core.run.context import load_observation_snapshot
from gui_agent.core.run.contracts import (
    StatementInvocation,
    build_read_statement,
)
from gui_agent.core.run.statements.binding import execute_read
from gui_agent.core.run.statements.compute_kernel import normalize_table_rows
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app
from gui_agent.context.runtime import set_provided_current_date


def _check_knowledge(run_dir: Path, platform: str) -> str:
    context_path = run_dir / "context.json"
    if not context_path.is_file():
        return ""
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    summary = raw.get("knowledge") or {}
    app_name = str(summary.get("app_name") or "")
    if not app_name:
        return ""
    knowledge = load_knowledge_for_app(
        app_name,
        platform,
        include_skills=summary.get("profile") not in {None, "functional-only"},
    )
    return knowledge.check if knowledge else ""


def _task_goal(run_dir: Path) -> str:
    context_path = run_dir / "context.json"
    if not context_path.is_file():
        return ""
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    return str(raw.get("goal") or raw.get("raw_input") or "")


def run_read_replay(
    run_dir: Path,
    *,
    turn: int,
    request: dict[str, Any],
    expectation: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    observation = load_observation_snapshot(
        run_dir / f"observation_turn_{turn}.json"
    )
    fields, field_types = field_projection(request["fields"])
    goal = _task_goal(run_dir)
    invocation = StatementInvocation(
        statement=build_read_statement(fields, field_types),
        task_goal=goal,
        inputs={"ui_state": {"token": "replay:state"}},
        args={"field_types": field_types},
    )
    platform = str(request.get("platform") or observation.source or "browser")
    bundle = build_platform(platform)
    outcome = execute_read(
        invocation,
        observation=observation,
        check_knowledge=_check_knowledge(run_dir, platform),
        prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
    )
    outputs = dict(outcome.outputs)
    if outcome.is_completed:
        outputs = normalize_table_rows([outputs], field_types)[0]
    result = {
        "source": str(run_dir),
        "turn": turn,
        "phase": outcome.phase,
        "verification": outcome.verification,
        "outputs": outputs,
    }
    failures = []
    for name in ("phase", "verification", "outputs"):
        expected = expectation.get(name)
        if expected is not None and result[name] != expected:
            failures.append(f"expected {name}={expected!r}, got {result[name]!r}")
    return result, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--expect-json", default="{}")
    parser.add_argument(
        "--date",
        default="",
        help="Seed the provided current date (YYYY-MM-DD) so relative dates resolve "
        "like production (the Android session seeds the device clock).",
    )
    args = parser.parse_args()

    if args.date:
        from datetime import date, datetime

        set_provided_current_date(
            datetime.combine(date.fromisoformat(args.date), datetime.min.time())
        )
    result, failures = run_read_replay(
        args.run_dir.resolve(),
        turn=args.turn,
        request=json.loads(args.request_json),
        expectation=json.loads(args.expect_json),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    for failure in failures:
        print(f"[FAIL] {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
