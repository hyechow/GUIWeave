"""Non-UI orchestrator primitive execution.

`read` and `data_query` are execution primitives, not UI actions. They consume the
current observation/table snapshot, record a non-interactive turn, and advance the
DSL interpreter without going through the supervisor/action-policy loop.

A `navigation` run whose (already-templated) target carries a concrete URL on a
device that exposes a browser-only ``navigate(url)`` is the same shape: a
deterministic jump, not a UI hunt. The canonical case is a foreach drill — the
row collection step collects each row's detail URL (table_reader folds cell hrefs into
``<col>_url`` sibling columns), then the loop visits ``{row[..._url]}`` directly +
structured-reads the landed page, with no per-row plan→act loop. Platforms with no
``navigate`` (iphone/android) and navigations without a URL fall through to the
supervisor unchanged.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from llm.structured import get_llm_call_count, get_llm_token_usage

from gui_agent.core.orchestrator.callframe import open_call, package_result
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
    observation: Observation | None = None
    observation_url: str | None = None
    # Feasibility Guard (non-UI kick-back): set when a data_query/read step FAILED in a re-plannable way
    # (data source empty / mismatched with the task intent) — the loop turns this into a re-decompose
    # directive instead of plainly ending the run. None = no re-plannable non-UI failure.
    failure_evidence: str | None = None


@dataclass
class _RepairAttempt:
    reads: dict[str, str] | None
    sql: str
    reason: str
    source_issue: str = ""


# A concrete http(s) URL embedded in a run's prose target. The CJK exclusion lets the URL end
# naturally at the Chinese text the planner wraps it in (e.g. "打开 https://h/id/5 的详情").
_URL_RE = re.compile(r"https?://[^\s一-鿿]+")
_BACK_NAV_RE = re.compile(r"返回|上一页|后退|\bback\b", re.IGNORECASE)



def _direct_nav_url(run: Run | None, platform: Any) -> str | None:
    """The URL to jump to if `run` is a deterministic, non-interactive navigation; else None.

    Requires: kind=navigation, a concrete URL already templated into the target name, and a device
    that exposes a browser-only ``navigate(url)``. A plain navigation (no URL) or a device that
    can't navigate by URL (iphone/android) returns None and goes through the supervisor as usual."""
    if run is None or run.kind != "navigation":
        return None
    if not callable(getattr(getattr(platform, "client", None), "navigate", None)):
        return None
    match = _URL_RE.search(run.name or "")
    if not match:
        return None
    return match.group(0).rstrip(").,;，。）") or None


def _direct_back(run: Run | None, platform: Any) -> bool:
    """True when a navigation run explicitly asks for browser history back."""
    if run is None or run.kind != "navigation":
        return False
    if not callable(getattr(getattr(platform, "client", None), "go_back", None)):
        return False
    return bool(_BACK_NAV_RE.search(run.name or ""))


def drive_pending_non_ui(
    *,
    current_run: Run | None,
    run_index: int,
    notes_mark: int,
    interpreter_steps: Any,
    bundle: Any,
    platform: Any,
    log_dir: Path,
    supervisor: Any,
    context: PolicyContext,
    save_context: Callable[[], None],
    say: Callable[[str], None],
    status: Callable[[str], None] | None = None,
    done_observation: Observation | None = None,
    observation_url: str | None = None,
    materialized_tables: "Callable[[], list[dict[str, Any]]] | None" = None,
    recovery: Any = None,  # RecoveryLedger(异常体系 Stage A):SQL 修复/数据源失败事件入账;None=不记
) -> NonUiDriveResult:
    """Execute consecutive `read` / `data_query` runs and advance the interpreter.

    `materialized_tables` = a PROVIDER returning the interpreter's foreach `into` tables (accumulated
    rows from iterating a collection). It's folded into a data_query's source so a query AFTER a
    foreach can analyze the whole collected set. It MUST be called fresh right before each data_query
    (not snapshotted at entry): a foreach's `into` table is populated DURING this drain loop — when the
    last body return completes the interpreter resumes, accumulates, and yields the data_query — so a
    value captured at entry is still empty (regression 20260622_215814: the query saw no table though
    foreach had read all rows)."""
    cur_run = current_run
    failure_evidence: str | None = None  # last re-plannable non-UI failure (for Feasibility Guard kick-back)
    nav_n = 0  # running count of URL-direct drills in THIS batch — surfaced to the HUD so a long drill
    #            (dozens of `直达导航` jumps inside one milestone hand-off) doesn't look frozen.
    direct_return_stack: list[str] = []

    def _hud(msg: str) -> None:
        # Non-UI primitives run inside a hand-off, NOT a top-level `--- Turn N ---`, so the main loop
        # never pushes a HUD status for them. Push one here so the HUD ticks per drill step. (max_turns
        # is untouched: these still record as non_interactive turns and don't consume the UI budget.)
        if status is not None:
            status(msg)

    obs = done_observation
    frame = getattr(obs, "png_bytes", None) if obs is not None else None
    tables = getattr(obs, "tables", None) if obs is not None else None
    obs_url = observation_url

    def ensure_observation() -> Observation:
        nonlocal obs, frame, tables, obs_url
        if obs is None:
            obs_url = f"screenshot_read_{run_index}.png"
            obs = bundle.make_perception(platform, log_dir / obs_url).observe()
            frame = getattr(obs, "png_bytes", None)
            tables = getattr(obs, "tables", None)
        return obs

    while cur_run is not None:
        if not (
            cur_run.is_query
            or _direct_nav_url(cur_run, platform) is not None
            or _direct_back(cur_run, platform)
        ):
            break
        run_for_turn = cur_run
        turn_started = time.perf_counter()
        calls_before = get_llm_call_count()
        tokens_before = get_llm_token_usage()
        context_reports: list[dict] = []
        reads: dict[str, str] = {}
        rows: list[dict[str, str]] = []
        completed = True
        summary = f"读取 {'、'.join(cur_run.returns) or cur_run.name}"
        executed_sql = getattr(cur_run, "sql", "")  # Query-only field (sibling IR)
        if cur_run.kind == "navigation" and _direct_back(cur_run, platform):
            nav_n += 1
            return_url = direct_return_stack.pop() if direct_return_stack else ""
            if return_url:
                _hud(f"直达返回 {nav_n}：回到 {return_url}")
                say(f"  [Orchestrator] 直达返回 {nav_n} · 导航回 {return_url}")
                platform.client.navigate(return_url)
            else:
                _hud(f"直达返回 {nav_n}：浏览器后退")
                say(f"  [Orchestrator] 直达返回 {nav_n} · 浏览器后退")
                platform.client.go_back()
            settle = getattr(platform.client, "wait_settled", None)
            if callable(settle):
                try:
                    settle("navigate" if return_url else "back")
                except Exception:  # noqa: BLE001 — settling is best-effort; observe regardless
                    pass
            obs_url = f"screenshot_back_{run_index}.png"
            obs = bundle.make_perception(platform, log_dir / obs_url).observe()
            frame = obs.png_bytes
            tables = obs.tables
            summary = "浏览器后退"
            executed_sql = ""
        elif cur_run.kind == "navigation":
            from gui_agent.core.orchestrator.url_json_read import read_json_url_returns

            nav_url = _direct_nav_url(cur_run, platform)  # non-None per the while condition
            nav_n += 1
            if obs is not None and getattr(obs, "url", None):
                direct_return_stack.append(str(obs.url))
            _hud(f"直达钻取 {nav_n}：打开 {nav_url}")
            say(f"  [Orchestrator] 直达钻取 {nav_n} · 直达导航 {nav_url}")
            platform.client.navigate(nav_url)
            settle = getattr(platform.client, "wait_settled", None)
            if callable(settle):
                try:
                    settle("navigate")
                except Exception:  # noqa: BLE001 — settling is best-effort; observe regardless
                    pass
            # The jump left the previous page: re-observe the landed page unconditionally.
            obs_url = f"screenshot_nav_{run_index}.png"
            obs = bundle.make_perception(platform, log_dir / obs_url).observe()
            frame = obs.png_bytes
            tables = obs.tables
            if cur_run.returns:
                json_reads = read_json_url_returns(cur_run.name, list(cur_run.returns), cur_run.read_spec)
                if json_reads is not None and any(str(json_reads.get(field, "")).strip() for field in cur_run.returns):
                    reads = json_reads
                    say(f"  [Orchestrator] 直达后 URL JSON 读取 {cur_run.returns} → {reads}")
                else:
                    from gui_agent.adapters.browser.page_read import read_page_complete
                    reads = read_page_complete(
                        obs,
                        list(cur_run.returns),
                        read_spec=cur_run.read_spec,
                        check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                        bundle=bundle,
                        platform=platform,
                        log_dir=log_dir,
                        context_reports=context_reports,
                    )
                    say(f"  [Orchestrator] 直达后读取 {cur_run.returns} → {reads}")
            summary = f"直达导航 {nav_url}"
            executed_sql = ""
        elif cur_run.kind == "read" and cur_run.returns:
            from gui_agent.core.orchestrator.url_json_read import read_json_url_returns

            _hud(f"读取验收帧 {'、'.join(cur_run.returns)}")
            json_reads = read_json_url_returns(cur_run.name, list(cur_run.returns), cur_run.read_spec)
            if json_reads is not None and any(str(json_reads.get(field, "")).strip() for field in cur_run.returns):
                reads = json_reads
                say(f"  [Orchestrator] URL JSON 读取 {cur_run.returns} → {reads}")
            else:
                if frame is None:
                    ensure_observation()
                from gui_agent.adapters.browser.page_read import read_page_complete
                reads = read_page_complete(
                    obs,
                    cur_run.returns,
                    read_spec=cur_run.read_spec,
                    check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                    bundle=bundle,
                    platform=platform,
                    log_dir=log_dir,
                    context_reports=context_reports,
                )
                say(f"  [Orchestrator] 只读验收帧 {cur_run.returns} → {reads}")
        elif cur_run.kind == "data_query":
            from gui_agent.core.orchestrator.data_query import DataQueryError, execute_data_query
            from gui_agent.core.orchestrator.data_query_repair import repair_data_query_sql

            _hud(f"数据查询 {'、'.join(cur_run.returns) or cur_run.name}")
            ensure_observation()
            query_tables = tables
            # Fold in foreach-accumulated tables (e.g. per-review rows collected by iterating a list):
            # they're a complete, in-memory data source the query references by its `into` var name.
            # Pulled FRESH here (not at entry) so the just-completed foreach's into table is included.
            mats = materialized_tables() if callable(materialized_tables) else (materialized_tables or [])
            if mats:
                # Foreach-materialized tables go FIRST so they become table_1/data in the profile.
                # The SQL references them by their `into` var name (e.g. "completed_orders"); if they
                # appear after the current-page DOM snapshot, the repair LLM sees the DOM's partial=True
                # table as table_1 and incorrectly rejects the query as data-source mismatch.
                query_tables = list(mats) + list(query_tables or [])

            def _try_repair(reason: str) -> _RepairAttempt | None:
                repair = repair_data_query_sql(
                    goal=context.goal or "",
                    run_name=cur_run.name,
                    requested_returns=list(cur_run.returns),
                    original_sql=getattr(cur_run, "sql", ""),
                    tables=query_tables,
                    failure=reason,
                    recent_ui_context=_recent_ui_context(context),
                )
                if repair is None:
                    return None
                if not getattr(repair, "source_ok", True):
                    issue = getattr(repair, "source_issue", "") or getattr(repair, "reason", "")
                    return _RepairAttempt(
                        reads=None,
                        sql="",
                        reason=getattr(repair, "reason", "") or issue,
                        source_issue=issue or "当前已采集表格与任务要求的数据源口径不一致，需要回到界面修正后再查询",
                    )
                repaired_reads = execute_data_query(
                    query_tables,
                    repair.sql,
                    cur_run.returns,
                    require_complete=getattr(cur_run, "data_scope", "complete") != "current",
                )
                return _RepairAttempt(
                    reads=repaired_reads,
                    sql=repair.sql,
                    reason=repair.reason,
                )

            def _rec(outcome: str, detail: str = "") -> None:
                # 恢复账本(Stage A):SQL 运行时修复是 data_source_error 类恢复机制,每次尝试入账。
                if recovery is not None:
                    recovery.record("data_source_error", "sql_repair",
                                    str(getattr(cur_run, "var", "") or cur_run.name),
                                    detail=detail[:200], outcome=outcome)

            try:
                reads = execute_data_query(
                    query_tables,
                    getattr(cur_run, "sql", ""),
                    cur_run.returns,
                    require_complete=getattr(cur_run, "data_scope", "complete") != "current",
                )
                if _empty_query_result(reads, cur_run.returns) and _has_query_rows(query_tables):
                    repair_result = _try_repair(f"原 SQL 在非空表格上返回空结果: {reads}")
                    if repair_result is not None:
                        if repair_result.source_issue:
                            completed = False
                            summary = f"数据源与任务意图不一致: {repair_result.source_issue}"
                            _rec("source_mismatch", repair_result.source_issue)
                            say(f"  [Orchestrator] 数据查询失败：{summary}")
                        elif repair_result.reads is not None and not _empty_query_result(repair_result.reads, cur_run.returns):
                            reads = repair_result.reads
                            executed_sql = repair_result.sql
                            _rec("recovered", repair_result.reason)
                            say(f"  [Orchestrator] 数据查询运行时修复：{repair_result.reason}")
                        else:
                            _rec("no_improvement", repair_result.reason)
                if completed:
                    summary = f"数据查询 {'、'.join(cur_run.returns) or cur_run.name}"
                    say(f"  [Orchestrator] 数据查询 {cur_run.returns} → {reads}")
            except DataQueryError as exc:
                repair_result = None
                repair_error: str | None = None
                if _has_query_rows(query_tables):
                    try:
                        repair_result = _try_repair(str(exc))
                    except DataQueryError as repair_exc:
                        repair_error = str(repair_exc)
                        repair_result = None
                if repair_result is not None and repair_result.source_issue:
                    completed = False
                    summary = f"数据源与任务意图不一致: {repair_result.source_issue}"
                    _rec("source_mismatch", repair_result.source_issue)
                    say(f"  [Orchestrator] 数据查询失败：{summary}")
                elif (
                    repair_result is not None
                    and repair_result.reads is not None
                    and not _empty_query_result(repair_result.reads, cur_run.returns)
                ):
                    reads = repair_result.reads
                    executed_sql = repair_result.sql
                    summary = f"数据查询 {'、'.join(cur_run.returns) or cur_run.name}"
                    _rec("recovered", repair_result.reason)
                    say(f"  [Orchestrator] 数据查询运行时修复：{repair_result.reason}")
                    say(f"  [Orchestrator] 数据查询 {cur_run.returns} → {reads}")
                else:
                    completed = False
                    if repair_result is not None and repair_result.reads is not None:
                        summary = f"SQL 修复后仍返回空结果: {repair_result.reads}"
                        _rec("no_improvement", summary)
                    elif repair_error:
                        summary = f"{exc}; SQL 修复也失败: {repair_error}"
                        _rec("repair_failed", repair_error)
                    else:
                        summary = str(exc)
                    say(f"  [Orchestrator] 数据查询失败：{exc}")
        # A data_query that failed because its data source is empty / mismatched with the task is
        # RE-PLANNABLE: capture it so the loop can kick back to the orchestrator (re-decompose) rather
        # than end the run. (read failures are not routed — they're per-frame, not a plan-shape issue.)
        if not completed and run_for_turn.kind == "data_query":
            failure_evidence = summary
            if recovery is not None:
                # 恢复账本(Stage A):可重排的 data_query 失败证据,是非 UI kickback 的前因。
                recovery.record("data_source_error", "data_query_failure",
                                str(getattr(run_for_turn, "var", "") or run_for_turn.name),
                                detail=str(summary)[:200], outcome="replan_candidate")
        result = package_result(
            run_for_turn,
            completed=completed,
            summary=summary,
            notes=[],
            reads=reads,
            rows=rows,
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
                sql=executed_sql,
                data_scope=getattr(run_for_turn, "data_scope", "complete"),
                reads=dict(reads),
                completed=completed,
                observation_url=obs_url or "",
                started_at=turn_started,
                llm_calls=get_llm_call_count() - calls_before,
                input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                llm_context=context_reports,
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
                observation=obs,
                observation_url=obs_url,
                failure_evidence=failure_evidence,
            )
        run_index += 1
        save_context()

    if cur_run is not None:
        milestone = open_call(
            supervisor,
            cur_run,
            run_index,
            fresh_advance=done_observation is not None and not bool(getattr(cur_run, "returns", None)),
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
        observation=obs,
        observation_url=obs_url,
    )


def _empty_query_result(reads: dict[str, str], returns: list[str]) -> bool:
    if not reads:
        return True
    fields = returns or list(reads)
    values = [str(reads.get(field, "")).strip().lower() for field in fields]
    return bool(values) and all(value in {"", "[]", "{}", "null", "none"} for value in values)


def _has_query_rows(tables: list[dict[str, Any]] | None) -> bool:
    for table in tables or []:
        if isinstance(table, dict) and table.get("rows"):
            return True
    return False


def _recent_ui_context(context: PolicyContext, *, limit: int = 6) -> str:
    lines: list[str] = []
    for turn in (context.turns or [])[-limit:]:
        supervisor = getattr(turn, "supervisor", None)
        if supervisor is not None:
            summary = getattr(supervisor, "summary", "") or ""
            if summary:
                lines.append(summary)
        checker = getattr(turn, "checker", None)
        if checker is not None:
            reason = getattr(checker, "reason", "") or ""
            summary = getattr(checker, "summary", "") or ""
            if reason:
                lines.append(reason)
            elif summary:
                lines.append(summary)
    return "\n".join(lines[-limit:])
