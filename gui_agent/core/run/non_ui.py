"""Non-UI orchestrator primitive execution.

`read` and `data_query` are execution primitives, not UI actions. They consume the
current observation/table snapshot, record a non-interactive turn, and advance the
DSL interpreter without going through the supervisor/action-policy loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.engine import package_result, task_type_for, to_milestone
from gui_agent.core.orchestrator.program import Run
from gui_agent.core.run.turns import make_non_ui_turn
from gui_agent.core.schemas import Observation, PolicyContext


@dataclass
class NonUiDriveResult:
    """State changes from driving pending non-UI runs."""

    current_run: Run | None
    run_index: int
    notes_mark: int
    reply: str | None = None


def drive_pending_non_ui(
    *,
    current_run: Run | None,
    run_index: int,
    notes_mark: int,
    interpreter_steps: Any,
    bundle: Any,
    phone: Any,
    log_dir: Path,
    supervisor: Any,
    context: PolicyContext,
    save_context: Callable[[], None],
    say: Callable[[str], None],
    done_observation: Observation | None = None,
    observation_url: str | None = None,
) -> NonUiDriveResult:
    """Execute consecutive `read` / `data_query` runs and advance the interpreter."""
    cur_run = current_run
    obs = done_observation
    frame = getattr(obs, "png_bytes", None) if obs is not None else None
    tables = getattr(obs, "tables", None) if obs is not None else None
    obs_url = observation_url

    def ensure_observation() -> Observation:
        nonlocal obs, frame, tables, obs_url
        if obs is None:
            obs_url = f"screenshot_read_{run_index}.png"
            obs = bundle.make_perception(phone, log_dir / obs_url).observe()
            frame = getattr(obs, "png_bytes", None)
            tables = getattr(obs, "tables", None)
        return obs

    while cur_run is not None and cur_run.kind in {"read", "data_query"}:
        run_for_turn = cur_run
        turn_started = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()
        reads: dict[str, str] = {}
        completed = True
        summary = f"读取 {'、'.join(cur_run.returns) or cur_run.name}"
        if cur_run.kind == "read" and cur_run.returns:
            from gui_agent.core.orchestrator.structured_read import structured_read

            if frame is None:
                ensure_observation()
            reads = structured_read(
                frame,
                cur_run.returns,
                read_spec=cur_run.read_spec,
                check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
            )
            say(f"  [Orchestrator] 只读验收帧 {cur_run.returns} → {reads}")
        elif cur_run.kind == "data_query":
            from gui_agent.core.orchestrator.data_query import DataQueryError, execute_data_query

            ensure_observation()
            try:
                reads = execute_data_query(
                    tables,
                    cur_run.sql,
                    cur_run.returns,
                    require_complete=getattr(cur_run, "data_scope", "complete") != "current",
                )
                summary = f"数据查询 {'、'.join(cur_run.returns) or cur_run.name}"
                say(f"  [Orchestrator] 数据查询 {cur_run.returns} → {reads}")
            except DataQueryError as exc:
                completed = False
                summary = str(exc)
                say(f"  [Orchestrator] 数据查询失败：{exc}")
        result = package_result(
            run_for_turn,
            completed=completed,
            summary=summary,
            notes=[],
            reads=reads,
        )
        milestone_id = run_for_turn.var or f"m{run_index}_{run_for_turn.kind}"
        if not any(m.get("id") == milestone_id for m in context.milestones):
            context.milestones.append(
                {
                    "id": milestone_id,
                    "name": run_for_turn.name,
                    "description": run_for_turn.name,
                    "kind": run_for_turn.kind,
                    "success_condition": summary,
                }
            )
        context.turns.append(
            make_non_ui_turn(
                index=len(context.turns) + 1,
                observation_source=getattr(obs, "source", "non_ui") if obs is not None else "non_ui",
                milestone_id=milestone_id,
                summary=summary,
                kind=run_for_turn.kind,
                name=run_for_turn.name,
                var=run_for_turn.var or "",
                returns=list(run_for_turn.returns),
                read_spec=run_for_turn.read_spec,
                sql=run_for_turn.sql,
                data_scope=getattr(run_for_turn, "data_scope", "complete"),
                reads=dict(reads),
                completed=completed,
                observation_url=obs_url or "",
                started_at=turn_started,
                llm_calls=get_llm_call_count() - calls_before,
                input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                output_tokens=get_llm_token_usage()[1] - tokens_before[1],
            )
        )
        try:
            cur_run = interpreter_steps.send(result)
        except StopIteration as exc:
            return NonUiDriveResult(
                current_run=None,
                run_index=run_index,
                notes_mark=notes_mark,
                reply=exc.value or "",
            )
        run_index += 1
        save_context()

    if cur_run is not None:
        milestone = to_milestone(cur_run, run_index)
        supervisor.reseed(
            milestone,
            task_type=task_type_for(cur_run),
            fresh_advance=done_observation is not None,
        )
        if not any(m.get("id") == milestone.id for m in context.milestones):
            context.milestones.append(
                {
                    "id": milestone.id,
                    "name": milestone.name,
                    "description": milestone.description,
                    "kind": milestone.kind,
                    "success_condition": milestone.success_condition,
                }
            )
        notes_mark = len(context.content_notes)
    return NonUiDriveResult(
        current_run=cur_run,
        run_index=run_index,
        notes_mark=notes_mark,
        reply=None,
    )
