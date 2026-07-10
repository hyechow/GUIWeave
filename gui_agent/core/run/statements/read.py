"""Current-observation Read executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from gui_agent.core.orchestrator.program import Read
from gui_agent.core.orchestrator.runner import make_run_result

from .observation import ObservationCursor
from .outcome import StatementOutcome


def execute_read(
    run: Read,
    *,
    statement_index: int,
    cursor: ObservationCursor,
    bundle: Any,
    platform: Any,
    log_dir: Path,
    check_knowledge: str,
    say: Callable[[str], None],
    status: Callable[[str], None],
) -> StatementOutcome:
    """Read declared fields without advancing the page or the Program interpreter."""
    reads: dict[str, str] = {}
    context_reports: list[dict] = []
    summary = f"读取 {'、'.join(run.returns) or run.name}"

    if run.returns:
        from gui_agent.core.orchestrator.primitives.url_json_read import read_json_url_returns

        status(f"读取验收帧 {'、'.join(run.returns)}")
        json_reads = read_json_url_returns(run.name, list(run.returns), run.read_spec)
        if json_reads is not None and any(
            str(json_reads.get(field, "")).strip() for field in run.returns
        ):
            reads = json_reads
            say(f"  [Orchestrator] URL JSON 读取 {run.returns} → {reads}")
        else:
            observation = cursor.ensure(statement_index)
            from gui_agent.adapters.browser.page_read import read_page_complete

            reads = read_page_complete(
                observation,
                run.returns,
                read_spec=run.read_spec,
                check_knowledge=check_knowledge,
                bundle=bundle,
                platform=platform,
                log_dir=log_dir,
                context_reports=context_reports,
            )
            say(f"  [Orchestrator] 只读验收帧 {run.returns} → {reads}")

    return StatementOutcome(
        result=make_run_result(
            run,
            completed=True,
            summary=summary,
            notes=[],
            reads=reads,
        ),
        summary=summary,
        observation=cursor.observation,
        observation_url=cursor.observation_url,
        context_reports=context_reports,
    )
