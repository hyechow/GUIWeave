"""Agent loop implementation for policy experiments."""

from __future__ import annotations

import sys
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, TYPE_CHECKING

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv()

from llm.structured import get_llm_call_count, get_llm_token_usage
from gui_agent.core.run.content import ReadState
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.llm.reader import ContentReader
from gui_agent.core.run.context import (
    load_context as _load_context,
    save_context as _save_context,
    save_observation_snapshot,
)
from gui_agent.core.run.result import (
    AgentResult,
    make_result as _make_result,
    orchestration_result as _orch_result,
)
from gui_agent.core.run.action_exec import (
    ActionExecutor,
    finalize_auto_continue_turn,
    submit_target_verify,
)
from gui_agent.core.run.flow import (
    evaluate_turn_progress,
    finish_terminal_step,
    handle_loading_frame,
)
from gui_agent.core.run.statements import drain_immediate_statements
from gui_agent.core.orchestrator.program import Interact, Program
from gui_agent.core.orchestrator.recovery import (
    MAX_EMPTY_RETURN_RECOVERIES,
    MAX_KICKBACK_REPLANS,
)
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.run.recovery_router import RecoveryRouter
from gui_agent.core.run.statements.outcome import StatementOutcome
from gui_agent.core.run.interactive import (
    contract_for_interact,
    extract_interact_outputs,
    project_interact_outputs,
    start_statement,
    statement_id,
    statement_info,
)
from gui_agent.core.run.turns import (
    emit_statement_fields,
    interactive_turn_count as _interactive_turn_count,
    make_statement_outcome_event,
    record_collection_slice,
    record_interactive_turn,
    sync_turn_metadata,
)
from gui_agent.core.supervisor.base import SupervisorPolicy
from gui_agent.core.llm.temporal import resolve_temporal_expressions
from gui_agent.core.policies.base import ActionPolicy
from gui_agent.core.schemas import PolicyContext
from gui_agent.core.run.state import sync_context_program_outcome

if TYPE_CHECKING:
    # Adapter types used only in annotations. With `from __future__ import
    # annotations` these stay lazy strings, so importing runner pulls in no
    # adapter at module top.
    from gui_agent.core.ui.hud import AgentHUD

TURN_HEADER = "\033[1;36m--- Turn {turn_no} ---\033[0m"

# 页面未稳定（白屏/加载中）的等待帧：不计入 max_turns、不累加 noop，只重新观察。
# 加载是 App 渲染延迟、不是 agent 的一步操作，不该消耗轮数预算；但要设上限防页面永挂死循环。
LOADING_WAIT_S = 0.6          # 每个加载帧重新观察前的等待，给页面渲染时间
MAX_LOADING_FRAMES = 12       # 连续加载帧上限，超过即判页面永挂、停止


def _needs_terminal_reconciliation(context: PolicyContext) -> bool:
    """Whether the hard limit landed immediately after an unresolved GUI dispatch."""
    if not context.journal.turns:
        return False
    latest = context.journal.turns[-1]
    signal = latest.action_signal
    return bool(
        latest.operation_mode == "interactive"
        and latest.executed
        and signal is not None
        and signal.execution == "dispatched"
    )


def _turn_budget_mode(
    context: PolicyContext,
    max_turns: int,
) -> Literal["normal", "reconcile", "stop"]:
    """Choose the next loop mode without ever increasing the caller's hard limit."""
    if _interactive_turn_count(context) + 1 <= max_turns:
        return "normal"
    if _needs_terminal_reconciliation(context):
        return "reconcile"
    return "stop"


# Post-action targeting verify runs in this 1-worker pool so it overlaps the
# settle wait (near-zero added latency). Daemon threads; finishes at process exit.
_VERIFY_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verify")

# YOLO detection + OCR run here, concurrent with the supervisor decide, so the
# snap has its boxes/text ready by execute time (~0.4s off the critical path).
_PREP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prep")

def build_policy(name: str) -> "ActionPolicy":
    # Selection routes through the platform bundle; the factory raises ValueError
    # with the available choices on an unknown name (same behavior as before).
    return build_platform().make_action_policy(name)


def build_supervisor(name: str) -> "SupervisorPolicy":
    return build_platform().make_supervisor(name)


def run_agent_loop(
    prompt: str,
    action_policy: ActionPolicy,
    supervisor: SupervisorPolicy,
    input_context_path: Path | None,
    log_dir: Path,
    context_path: Path,
    *,
    program: Program,
    max_turns: int = 20,
    auto_continue: bool = False,
    hud: AgentHUD | None = None,
    live_state: dict | None = None,
    silent: bool = False,
    backend: str | None = None,
    on_turn: object = None,  # callable(entry: dict) called after each turn
    raw_input: str | None = None,  # original human input; defaults to `prompt` (bin/runner)
    router: dict | None = None,    # RouterResult dict (chat path); None for bin/runner
    on_session_open: object = None,  # callable(platform) run once after session open, before the loop
    knowledge: dict | None = None,  # injected app-knowledge summary {app_name, nav_chars, ...}; None if no match
    redecompose: object = None,  # callable(directive:str)->Program|None; Program-level hot recompile.
    orchestrator_context_reports: list[dict] | None = None,
    stop_requested: object = None,  # callable() -> bool; true means stop after current turn settles
    platform: object = None,  # already-open session (runner pre-opens it so router/decompose can see the current front-tab url/title; see cli.py); None → open here (chat path, unchanged)
    headless: bool = False,  # suppress the action visualizer (cursor/overlay) on every platform; HUD is gated by the caller
) -> AgentResult:
    if not isinstance(program, Program):
        raise TypeError("run_agent_loop requires a compiled DSL Program")
    _run_started = time.perf_counter()  # for context.wall_clock_s (true end-to-end elapsed)

    def _say(s: str) -> None:
        if not silent:
            print(s)

    def _status(turn_no: int, msg: str) -> None:
        if hud:
            hud.update(f"Turn {turn_no} — {msg}")
        if live_state:
            live_state["current"] = msg

    def _stop_requested() -> bool:
        return bool(stop_requested and callable(stop_requested) and stop_requested())

    context = _load_context(
        input_context_path or context_path,
        resolve_temporal_expressions(prompt),
        supervisor.name,
        action_policy.name,
        raw_input=raw_input if raw_input is not None else prompt,
        router=router,
    )
    if knowledge is not None:
        context.knowledge = knowledge
    _prior_wall_clock_s = float(context.wall_clock_s or 0.0)

    # Bound after ProgramRuntime.start; _save_ctx / _finish close over this name.
    rt: ProgramRuntime | None = None

    def _save_ctx() -> None:
        """Persist context, stamping the run's wall-clock elapsed so far — so the final file
        carries the true end-to-end time (LLM + settle + perception/execution/overhead), not
        just the sum of LLM-module timings."""
        context.wall_clock_s = _prior_wall_clock_s + time.perf_counter() - _run_started
        _save_context(context_path, context)

    def _finish(result: AgentResult) -> AgentResult:
        if isinstance(context.orchestrator, dict):
            # Final report projection only. Runtime decisions and checkpoint replay read the
            # interpreter/EventJournal directly; no live run_log mirror is persisted per turn.
            report_run_log = (result.orchestrator or {}).get("run_log")
            if report_run_log is not None:
                context.orchestrator = {
                    **context.orchestrator,
                    "report_run_log": report_run_log,
                }
        if (
            rt is not None
            and rt.has_recovery
            and isinstance(context.orchestrator, dict)
        ):
            context.orchestrator = {
                **context.orchestrator,
                "recovery": rt.recovery_summary(),
            }
        sync_context_program_outcome(context, result)
        _save_ctx()
        return result

    _save_ctx()
    _say(f"Goal    : {context.goal}")
    _say(f"Turns   : {len(context.journal.turns)}")
    # Pin the task goal as a persistent HUD header (above the live turn status), so
    # the floating panel over the browser/mirror shows WHAT the agent is doing.
    if hud is not None and hasattr(hud, "set_goal"):
        hud.set_goal(context.goal)

    action_executor = ActionExecutor()
    original_goal = context.goal
    loading_streak = 0

    # The platform bundle is the single seam through which the agent loop obtains
    # the session, executor and perception — no adapter
    # class is referenced directly here.
    bundle = build_platform(backend=backend)
    reader = ContentReader(prepare_vision_prompt_png=bundle.prepare_vision_prompt_png)
    read_state = ReadState(context=context, reader=reader)
    # Record the platform on the context so the log and the HTML report can label
    # the run (iphone vs browser). Set here so both the runner and chat (which both
    # call run_agent_loop) persist it.
    if context.platform != bundle.platform:
        context.platform = bundle.platform
        _save_ctx()

    # Pre-session environment check (mirror open / CDP up / adb+ADBKeyboard ready). Runs
    # ONCE here before the session opens — UNLESS the caller already opened `platform` (the
    # runner pre-opens it so router/decompose can see the current front-tab url; see cli.py).
    # Any one-time setup a platform needs (android switching the IME to ADBKeyboard) happens
    # inside the check.
    if platform is None:
        setup = bundle.setup_check()
        for _line in setup.lines:
            _say(_line)
        if not setup.ok:
            _say(f"\n环境检查未通过：{setup.summary}")
            return _finish(
                _make_result(
                    context,
                    f"环境检查未通过：{setup.summary}",
                    phase="failed",
                )
            )
        session_cm = bundle.open_session()
    else:
        session_cm = nullcontext(platform)

    with session_cm as platform:
        executor = bundle.make_executor(platform)
        # Optional action visualizer (cursor/overlay). None when the platform has
        # none (iphone today) OR in headless mode (unified switch — no overlay on any
        # platform); show_action is best-effort before execute and may be called again
        # after executor snap updates the action point.
        visualizer = None if headless else bundle.make_action_visualizer(platform)

        def _flash(a) -> None:
            # Best-effort cursor/overlay flash; never raises into the loop.
            # No-op when the platform has no visualizer (iphone).
            if visualizer is not None and a is not None:
                try:
                    visualizer.show_action(a)
                except Exception:
                    pass

        # One-shot post-open hook (before the first observe): lets a caller prime
        # the just-connected session — e.g. inject auth cookies, start HAR capture,
        # and navigate to the task start_url. Runs on the neutral
        # `platform` (device at platform.client); default None keeps iphone/chat untouched.
        # NOT wrapped in try: an opt-in caller wants a failed prime (bad cookies /
        # unreachable start_url) to surface, not run the task in a wrong state.
        if on_session_open is not None and callable(on_session_open):
            on_session_open(platform)

        # If the device exposes its exact window rect (browser via CDP), pin the HUD
        # into that window now that we are connected — the pre-connect CGWindowList
        # guess can pick the wrong same-named Chrome window. Same placement helper as
        # the factory (centered, ≈ iOS dock height), so the position is consistent.
        if hud is not None and hasattr(hud, "reposition"):
            _client = getattr(platform, "client", None)
            _wb = _client.window_bounds() if hasattr(_client, "window_bounds") else None
            if _wb:
                from gui_agent.core.ui.hud import dock_rect
                hud.reposition(*dock_rect(*_wb))

        # ── ProgramRuntime (always on; sole statement-scheduling owner) ───────────
        _immediate_failure: "str | None" = None
        _immediate_kickback: "str | None" = None
        rt = ProgramRuntime.resume(program, context.journal)
        recovery_router = RecoveryRouter()
        if rt.current_instance_id:
            if rt.current is None or not isinstance(rt.current.statement, Interact):
                raise ValueError(
                    "journal resumed an active instance that is not an interactive statement"
                )
            resume_statement = getattr(supervisor, "resume_statement", None)
            if not callable(resume_statement):
                raise TypeError("configured supervisor cannot resume statement runtime")
            latest_snapshot = next(
                (
                    turn.runtime_state
                    for turn in reversed(context.journal.turns)
                    if turn.statement_instance_id == rt.current_instance_id
                    and turn.runtime_state is not None
                ),
                None,
            )
            if latest_snapshot is not None:
                rt.restore_current_contract(latest_snapshot.contract)
            resume_statement(
                contract_for_interact(rt.current, rt.index),
                instance_id=rt.current_instance_id,
                history=context.journal.turns,
            )
            _say(f"  [Resume] 恢复 statement {rt.current_instance_id}")
        _record_llm_mark = get_llm_call_count()
        _record_token_mark = get_llm_token_usage()

        def _stop_after_esc(turn_no: int) -> AgentResult | None:
            if not _stop_requested():
                return None
            _say("\n收到 ESC：当前 turn 已收尾，agent-loop 安全停止")
            reason = "用户按 ESC 中止 agent-loop"
            return _finish(_orch_result(
                context, rt.interpreter, f"{reason}（任务未完成）", current=rt.current,
            ))

        def _read_completed_outputs(invocation, observation) -> dict:
            """Project an Interact's typed outputs.

            ``list[record]`` returns declared ``complete``/``best_effort`` come from the
            Journal-projected CollectionView; the current frame was appended as a slice before
            Transition. ``current_view`` and scalar returns still come from the terminal frame.
            """
            return project_interact_outputs(
                invocation,
                observation,
                history=context.journal.events,
                instance_id=getattr(rt, "current_instance_id", "") or "",
                check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                say=_say,
            )

        def _drain_immediate(
            observation_for_statements=None,
            observation_url: str | None = None,
            *,
            allow_navigation: bool = True,
        ) -> "str | None":
            """Execute inline statements, then begin the next interactive statement."""
            nonlocal _immediate_failure, _immediate_kickback
            nonlocal _record_llm_mark, _record_token_mark
            nonlocal observation, observation_url_for_turn, prep_future
            result = drain_immediate_statements(
                program_runtime=rt,
                bundle=bundle,
                platform=platform,
                log_dir=log_dir,
                check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                context=context,
                save_context=_save_ctx,
                say=_say,
                status=lambda msg: _status(max(1, _interactive_turn_count(context)), msg),
                observation=observation_for_statements,
                observation_url=observation_url,
                allow_navigation=allow_navigation,
            )
            # Immediate statements record their own turns and metrics.
            _record_llm_mark = get_llm_call_count()
            _record_token_mark = get_llm_token_usage()
            if (
                rt.current is not None
                and isinstance(rt.current.statement, Interact)
                and getattr(supervisor, "_statement_rt", None) is None
            ):
                sid = statement_id(rt.current, rt.index)
                iid = rt.next_instance_id(sid)
                start_statement(
                    supervisor,
                    rt.current,
                    rt.index,
                    instance_id=iid,
                )
                rt.mark_notes(len(context.journal.content_notes))
            _immediate_failure = result.failure_evidence
            _immediate_kickback = result.replan_directive
            if result.observation is not None:
                observation = result.observation
                observation_url_for_turn = result.observation_url or observation_url_for_turn
                prep_future = _PREP_POOL.submit(executor.prepare_frame, observation.png_bytes)
            return result.reply

        def _finish_statement(
            *,
            turn_no: int,
            outcome: StatementOutcome,
        ) -> dict:
            """Terminal interactive statement → ProgramRuntime.send → task result."""
            return finish_terminal_step(
                outcome=outcome,
                read_state=read_state,
                turn_no=turn_no,
                program_runtime=rt,
                context=context,
                finish=_finish,
                say=_say,
                end_statement=supervisor.end_statement,
            )

        def _outcome_from_step(sv_step, *, outputs=None):
            outcome = sv_step.outcome
            if outcome is None:
                return None
            updates: dict = {"outputs": outputs} if outputs is not None else {}
            return outcome.model_copy(update=updates) if updates else outcome

        def _record_statement_outcome(sv_step, outcome) -> None:
            """Persist one terminal fact before Program advance/runtime teardown.

            The verdict frame is referenced by the event but does not become an
            action-less PolicyTurn. Resetting the LLM marks here also attributes a
            same-frame next-statement decision to the next real turn.
            """
            nonlocal _record_llm_mark, _record_token_mark
            calls_after = get_llm_call_count()
            tokens_after = get_llm_token_usage()
            _info, _iid = emit_statement_fields(supervisor)
            if not _iid and rt.current is not None:
                _info = statement_info(rt.current, rt.index)
                _iid = rt.current_instance_id
            context.journal.append_statement_outcome(make_statement_outcome_event(
                after_turn=len(context.journal.turns),
                observation_source=observation.source,
                observation_url=observation_url_for_turn,
                supervisor_step=sv_step,
                supervisor=supervisor,
                outcome=outcome,
                llm_calls=calls_after - _record_llm_mark,
                input_tokens=tokens_after[0] - _record_token_mark[0],
                output_tokens=tokens_after[1] - _record_token_mark[1],
                statement=_info,
                statement_instance_id=_iid,
            ))
            _record_llm_mark = calls_after
            _record_token_mark = tokens_after
            _save_ctx()

        _orchestrator_reports = list(orchestrator_context_reports or [])
        _orchestrator_metrics = next(
            (
                report for report in _orchestrator_reports
                if isinstance(report, dict) and report.get("kind") == "orchestrator_metrics"
            ),
            {},
        )
        _prior_orchestrator = context.orchestrator or {}
        context.orchestrator = {
            **_prior_orchestrator,
            "program": rt.program.model_dump(mode="json"),
            "max_turns": max_turns,
            "context_reports": (
                _prior_orchestrator.get("context_reports") or _orchestrator_reports
            ),
            "timings": (
                _prior_orchestrator.get("timings")
                or dict(_orchestrator_metrics.get("timings") or {})
            ),
            "token_usage": (
                _prior_orchestrator.get("token_usage")
                or dict(_orchestrator_metrics.get("token_usage") or {})
            ),
            "llm_calls": int(
                _prior_orchestrator.get("llm_calls")
                or _orchestrator_metrics.get("llm_calls")
                or 0
            ),
        }
        context.outcome = None
        _save_ctx()
        if rt.finished:
            return _finish(_orch_result(context, rt.interpreter, rt.reply or ""))
        _reply = _drain_immediate()
        if _reply is not None:
            return _finish(_orch_result(context, rt.interpreter, _reply))

        def _perform_replan(
            directive: str,
            observation=None,
            *,
            cls: str = "infeasible_route",
            terminal_outcome: StatementOutcome | None = None,
        ) -> "tuple[bool, str | None]":
            """Re-decompose remaining plan; hot-swap via ProgramRuntime.replace_program."""
            if not (directive and callable(redecompose)):
                return (False, None)
            kick_n = rt.begin_kickback()
            if kick_n is None:
                return (False, None)
            failed_run = rt.current
            _site = failed_run.id if failed_run is not None else "program"
            _say(f"\n[Kickback] 重规划 ({kick_n}/{MAX_KICKBACK_REPLANS})：{directive[:120]}")
            _rd_calls0 = get_llm_call_count()
            _rd_tok0 = get_llm_token_usage()
            _rd_t0 = time.perf_counter()
            _rd_reports: list = []
            from gui_agent.core.orchestrator import summarize_progress

            _experience, _remaining = summarize_progress(
                rt.program, rt.interpreter.run_log, failed_run,
            )
            _prev_log_len = len(rt.interpreter.run_log)
            if _experience:
                _say(
                    f"  [Kickback] 已执行经验 {_prev_log_len} 步、剩余目标若干"
                    " → 仅重排剩余（带经验+当前页面）"
                )
            try:
                _new = redecompose(
                    directive, _rd_reports,
                    observation=observation,
                    prior_experience=_experience,
                    remaining_plan=_remaining,
                )  # type: ignore[operator]
            except Exception as _exc:  # noqa: BLE001
                _say(f"[Kickback] 重规划失败（{_exc}），按原结果收尾")
                rt.record_recovery(
                    cls, "kickback_redecompose", _site,
                    detail=str(_exc)[:200], outcome="redecompose_failed",
                )
                return (False, None)
            if _new is None or not _new.statements:
                rt.record_recovery(cls, "kickback_redecompose", _site, outcome="no_plan")
                return (False, None)

            rt.record_recovery(
                cls, "kickback_redecompose", _site,
                detail=directive[:160], outcome="replanned",
            )
            # Close the infeasible statement's runtime BEFORE hot-swapping the program — the
            # infeasible step was already recorded as an interactive turn (Transition captured), and
            # the new program's first statement is begun by _drain_immediate below. Ending here
            # prevents the begin_statement guard from rejecting the implicit overwrite.
            if terminal_outcome is not None:
                supervisor.end_statement(terminal_outcome)
            rt.replace_program(
                _new,
                drop_failed_from_log=True,
                reason=directive,
                terminal_disposition=(
                    "abandon" if terminal_outcome is not None else "record_then_drop"
                ),
            )
            _prior = context.orchestrator or {}
            _redecomps = list(_prior.get("redecomposes") or [])
            _redecomps.append({
                "kickback_n": kick_n,
                "directive": directive,
                "at_turn": len(context.journal.turns),
                "program": _new.model_dump(mode="json"),
                "llm_calls": get_llm_call_count() - _rd_calls0,
                "token_usage": {"orchestrator.redecompose": {
                    "input": get_llm_token_usage()[0] - _rd_tok0[0],
                    "output": get_llm_token_usage()[1] - _rd_tok0[1],
                }},
                "timings": {"orchestrator.redecompose": time.perf_counter() - _rd_t0},
                "context_reports": _rd_reports,
            })
            context.orchestrator = {
                **_prior,
                "program": rt.program.model_dump(mode="json"),
                "redecomposes": _redecomps,
                "replanned_from_kickback": kick_n,
            }
            _save_ctx()
            if rt.finished:
                return (True, rt.reply or "")
            _reply2 = _drain_immediate()
            return (True, _reply2) if _reply2 is not None else (True, None)

        while True:
            interrupted = _stop_after_esc(_interactive_turn_count(context))
            if interrupted is not None:
                return interrupted

            turn_no = len(context.journal.turns) + 1
            _budget_mode = _turn_budget_mode(context, max_turns)
            _budget_reconcile = _budget_mode == "reconcile"
            if _budget_mode == "stop":
                _say(f"\n达到最大轮数 {max_turns}，agent-loop 停止")
                return _finish(_orch_result(
                    context, rt.interpreter,
                    f"达到最大轮数 {max_turns}（任务未完成）",
                    current=rt.current,
                ))
            if _budget_reconcile:
                _say(
                    f"\n达到最大轮数 {max_turns}；末次动作尚无后续观察，"
                    "执行一次无动作终态仲裁"
                )

            turn_started_at = time.perf_counter()
            llm_calls_before = get_llm_call_count()
            tokens_before = get_llm_token_usage()
            _record_llm_mark = llm_calls_before
            _record_token_mark = tokens_before

            _say("\n" + TURN_HEADER.format(turn_no=turn_no))

            # Observe a fresh frame each turn. A statement hand-off (the verdict-frame carry-
            # forward: deciding the next statement on the SAME frame the prior one was accepted
            # on, preserving transient hints + saving a screenshot) is now done WITHIN the turn
            # by the decision-phase loop below — it no longer crosses a turn boundary.
            _status(turn_no, "截图分析中…")
            observation_url_for_turn = f"screenshot_turn_{turn_no}.png"
            perception = bundle.make_perception(platform, log_dir / observation_url_for_turn)
            observation = perception.observe()
            save_observation_snapshot(
                log_dir / f"observation_turn_{turn_no}.json",
                observation,
                screenshot=observation_url_for_turn,
            )
            # YOLO + OCR run in the background, overlapping the decide below;
            # awaited just before execute (snap) so they add ~no latency.
            prep_future = _PREP_POOL.submit(executor.prepare_frame, observation.png_bytes)

            # ── Decision phase (same-frame statement hand-off) ────────────────────────
            # Decide for the current statement on THIS frame. A statement
            # that COMPLETES here is a hand-off, not a turn end: package it, advance the
            # interpreter (driving any read runs off this verdict frame), begin the next
            # statement, and re-decide on the SAME frame — so a pure hand-off never
            # costs its own action-less turn. Only an action or a stop ends the turn. The next
            # statement's nav skip-check is set when _drain_immediate seeds its contract.
            # Behaviorally the next statement is decided on the exact frame the prior one was accepted on — same
            # as the verdict-frame carry-forward, just merged into this turn instead of the next.
            _orch_reply: "str | None" = None    # set if the program ended during a hand-off
            _did_loading = False
            _did_kickback_replan = False
            _did_return_recovery = False
            # Each completed step writes its own timing/token diagnostics into the
            # OutcomeEvent before the next same-frame step clears supervisor state.
            while True:
                _status(turn_no, f"使用 {supervisor.name} supervisor 决策中…")
                _say("监督决策中...")
                # Observation facts precede decisions. A statement completing on this frame
                # therefore persists its terminal collection slice before Transition proposes
                # completion and before outputs are materialized.
                record_collection_slice(
                    context=context,
                    supervisor=supervisor,
                    observation=observation,
                    observation_url=observation_url_for_turn,
                    save_context=_save_ctx,
                )
                if _budget_reconcile:
                    sv_step = supervisor.reconcile(
                        observation, context.goal, context.journal.events
                    )
                else:
                    sv_step = supervisor.step(observation, context.goal, context.journal.events)
                _say(f"监督者: {sv_step.summary}")
                _status(turn_no, sv_step.summary)

                if sv_step.is_loading:
                    _did_loading = True
                    break
                # Terminal StatementOutcome drives hand-off; mid-loop → turn body.
                _step_outcome = _outcome_from_step(sv_step)
                if (
                    _step_outcome is None
                    or recovery_router.route_statement(_step_outcome).action
                    != "advance_program"
                ):
                    break  # act/observe/wait or non-completed terminal → turn body

                # Statement completed → hand off to the next statement, same frame.
                assert rt.current is not None
                _done_name = rt.current.goal
                _outputs = _read_completed_outputs(rt.current, observation)
                _missing = [
                    name
                    for name, spec in rt.current.statement.returns.items()
                    if spec.required and _outputs.get(name) in (None, "", [], {})
                ]
                _recovery_decision = recovery_router.route_statement(
                    _step_outcome,
                    return_violation=bool(_missing),
                    can_redecompose=callable(redecompose),
                )
                if _recovery_decision.action == "tighten_return":
                    _attempt = rt.next_return_attempt()
                    _cv_site = rt.current.id
                    if _attempt is not None:
                        rt.record_recovery(
                            "contract_violation", "tighten_return", _cv_site,
                            detail=f"missing outputs: {_missing}",
                            outcome=f"tighten {_attempt}/{MAX_EMPTY_RETURN_RECOVERIES}",
                        )
                        contract = contract_for_interact(rt.current, rt.index)
                        recovery = (
                            f"\n返回字段恢复尝试 {_attempt}：仅当 {_missing} 都可从当前界面"
                            "明确读取时才提议完成；否则继续在本 statement 内定位。"
                        )
                        supervisor.reset_for_return_retry(contract.model_copy(update={
                            "success": contract.success + recovery,
                        }))
                        _say(
                            "  [Orchestrator] 返回值合同未满足，继续定位"
                            f"（{_attempt}/{MAX_EMPTY_RETURN_RECOVERIES}）："
                            + "、".join(_missing)
                        )
                        _did_return_recovery = True
                    else:
                        rt.record_recovery(
                            "contract_violation", "tighten_return", _cv_site,
                            detail=f"missing outputs: {_missing}",
                            outcome="exhausted_honest_fail",
                        )
                        _say(
                            "  [Orchestrator] 返回值合同持续未满足，停止推进："
                            + "、".join(_missing)
                        )
                        _outcome = StatementOutcome.exhausted(
                            "返回值合同未满足：" + "、".join(_missing),
                            outputs=_outputs,
                        )
                        _record_statement_outcome(sv_step, _outcome)
                        try:
                            rt.send_outcome(_outcome)
                        finally:
                            supervisor.end_statement(_outcome)
                        if rt.finished:
                            _orch_reply = rt.reply or ""
                            break
                        # ALWAYS begin the next statement (was skipped when nxt was not None,
                        # leaving no live runtime → next step() raised). Mark return-recovery so
                        # the outer loop continues onto the newly-begun statement next turn.
                        _drain_immediate(
                            observation_for_statements=observation,
                            observation_url=observation_url_for_turn,
                            allow_navigation=not _budget_reconcile,
                        )
                        _did_return_recovery = True
                    break
                _outcome = _outcome_from_step(
                    sv_step,
                    outputs=_outputs,
                )
                assert _outcome is not None and _outcome.is_completed
                _record_statement_outcome(sv_step, _outcome)
                try:
                    rt.send_outcome(_outcome)
                finally:
                    supervisor.end_statement(_outcome)
                if rt.finished:
                    _orch_reply = rt.reply or ""
                    break
                _reply = _drain_immediate(
                    observation_for_statements=observation,
                    observation_url=observation_url_for_turn,
                    allow_navigation=not _budget_reconcile,
                )
                if _reply is not None:
                    _orch_reply = _reply
                    break
                assert rt.current is not None
                _say(
                    f"  [Orchestrator] 子目标「{_done_name}」完成 → 下一子任务："
                    f"{rt.current.goal}（同一验收帧上决策，不另起 turn）"
                )

            # The program ended on a hand-off (last actionable statement done → read / finish).
            # Every completed statement already recorded its own terminal event inside the
            # loop above, so no separate program-end verdict is needed here.
            if _orch_reply is not None:
                # An executor can report a structural contract/data-source conflict that the
                # current Program cannot resolve. Route that evidence to Program-level hot
                # recompile instead of treating it as an action-level rejection.
                _program_end_decision = recovery_router.route_program_end(
                    replan_directive=_immediate_kickback,
                    can_redecompose=callable(redecompose),
                )
                if _program_end_decision.action == "kickback":
                    assert _program_end_decision.recovery_class is not None
                    _directive = _immediate_kickback or ""
                    _handled, _r = _perform_replan(
                        _directive,
                        observation,
                        cls=_program_end_decision.recovery_class,
                    )
                    if _handled and _r is not None:
                        return _finish(_orch_result(context, rt.interpreter, _r))
                    if _handled:
                        _immediate_failure = None
                        _immediate_kickback = None
                        continue
                return _finish(_orch_result(context, rt.interpreter, _orch_reply))

            if _budget_reconcile and not _did_loading:
                _budget_outcome = _outcome_from_step(sv_step)
                if _budget_outcome is None:
                    raise RuntimeError(
                        "terminal-only reconciliation returned a running step"
                    )
                _record_statement_outcome(sv_step, _budget_outcome)
                return _finish_statement(
                    turn_no=turn_no,
                    outcome=_budget_outcome,
                )

            if _did_kickback_replan or _did_return_recovery:
                continue

            # 页面未稳定（白屏/加载中）：等待并重新观察，本帧不写入 context.journal.turns、不消耗
            # Loading is an adapter fact, not a model no-op: wait without writing a turn.
            if _did_loading:
                loading = handle_loading_frame(
                    loading_streak=loading_streak, max_loading_frames=MAX_LOADING_FRAMES,
                    wait_s=LOADING_WAIT_S, turn_no=turn_no,
                    current_run=rt.current, context=context, interpreter=rt.interpreter,
                    finish=_finish, stop_after_esc=_stop_after_esc, say=_say,
                )
                loading_streak = loading.streak
                if loading.terminal_result is not None:
                    return loading.terminal_result
                if loading.continue_loop:
                    continue
            loading_streak = 0

            interrupted = _stop_after_esc(turn_no)
            if interrupted is not None:
                return interrupted

            # A terminal verdict is a journal fact, not an action-less PolicyTurn.
            # Persist it before recovery/program advancement while statement-local
            # Transition and identity are still available.
            _turn_outcome = _outcome_from_step(sv_step)
            if _turn_outcome is not None:
                _record_statement_outcome(sv_step, _turn_outcome)
                _terminal_decision = recovery_router.route_statement(
                    _turn_outcome,
                    can_redecompose=callable(redecompose),
                )
                if _terminal_decision.action == "kickback":
                    assert _terminal_decision.recovery_class is not None
                    _say("\n[Kickback] statement 判定不可行 → 重规划")
                    _handled, _reply = _perform_replan(
                        _turn_outcome.kickback or "",
                        observation,
                        cls=_terminal_decision.recovery_class,
                        terminal_outcome=_turn_outcome,
                    )
                    if _handled and _reply is not None:
                        return _finish(_orch_result(context, rt.interpreter, _reply))
                    if _handled:
                        continue
                return _finish_statement(
                    turn_no=turn_no,
                    outcome=_turn_outcome,
                )

            _same_frame_replan = getattr(
                supervisor,
                "replan_after_action_rejection",
                None,
            )
            action_result = action_executor.run(
                sv_step=sv_step,
                observation=observation,
                action_policy=action_policy,
                supervisor=supervisor,
                executor=executor,
                prep_future=prep_future,
                log_dir=log_dir,
                turn_no=turn_no,
                flash=_flash,
                status=_status,
                say=_say,
                stop_requested=_stop_requested,
                replan=(
                    lambda rejected_step, reason: supervisor.replan_after_action_rejection(
                        observation,
                        context.goal,
                        context.journal.events,
                        rejected_step,
                        reason,
                    )
                )
                if callable(_same_frame_replan)
                else None,
            )
            if action_result.supervisor_step is not None:
                sv_step = action_result.supervisor_step
                _say(f"监督者重决策: {sv_step.summary}")
                _status(turn_no, sv_step.summary)

            # A grounding veto can cause the same-frame Transition replan to discover a terminal
            # verdict. Persist it as an OutcomeEvent, never as an action-less PolicyTurn.
            _post_grounding_outcome = _outcome_from_step(sv_step)
            if _post_grounding_outcome is not None:
                if _post_grounding_outcome.is_completed:
                    assert rt.current is not None
                    _outputs = _read_completed_outputs(rt.current, observation)
                    _missing = [
                        name
                        for name, spec in rt.current.statement.returns.items()
                        if spec.required and _outputs.get(name) in (None, "", [], {})
                    ]
                    if _missing:
                        _attempt = rt.next_return_attempt()
                        if _attempt is not None:
                            contract = contract_for_interact(rt.current, rt.index)
                            recovery = (
                                f"\n返回字段恢复尝试 {_attempt}：仅当 {_missing} 都可从当前界面"
                                "明确读取时才提议完成。"
                            )
                            supervisor.reset_for_return_retry(contract.model_copy(update={
                                "success": contract.success + recovery,
                            }))
                            _say(
                                "  [Orchestrator] 同帧重决策完成但返回值合同未满足，"
                                f"继续定位（{_attempt}/{MAX_EMPTY_RETURN_RECOVERIES}）："
                                + "、".join(_missing)
                            )
                            continue
                        _post_grounding_outcome = StatementOutcome.exhausted(
                            "返回值合同未满足：" + "、".join(_missing),
                            outputs=_outputs,
                        )
                    else:
                        _post_grounding_outcome = _outcome_from_step(
                            sv_step,
                            outputs=_outputs,
                        )
                _record_statement_outcome(sv_step, _post_grounding_outcome)
                _post_decision = recovery_router.route_statement(
                    _post_grounding_outcome,
                    can_redecompose=callable(redecompose),
                )
                if _post_decision.action == "kickback":
                    assert _post_decision.recovery_class is not None
                    _handled, _reply = _perform_replan(
                        _post_grounding_outcome.kickback or "",
                        observation,
                        cls=_post_decision.recovery_class,
                        terminal_outcome=_post_grounding_outcome,
                    )
                    if _handled and _reply is not None:
                        return _finish(_orch_result(context, rt.interpreter, _reply))
                    if _handled:
                        continue
                if _post_grounding_outcome.is_completed:
                    try:
                        rt.send_outcome(_post_grounding_outcome)
                    finally:
                        supervisor.end_statement(_post_grounding_outcome)
                    if rt.finished:
                        return _finish(
                            _orch_result(
                                context,
                                rt.interpreter,
                                rt.reply or _post_grounding_outcome.summary,
                            )
                        )
                    _reply = _drain_immediate(
                        observation_for_statements=observation,
                        observation_url=observation_url_for_turn,
                        allow_navigation=not _budget_reconcile,
                    )
                    if _reply is not None:
                        return _finish(_orch_result(context, rt.interpreter, _reply))
                    continue
                return _finish_statement(
                    turn_no=turn_no,
                    outcome=_post_grounding_outcome,
                )

            sync_turn_metadata(
                context=context,
                supervisor=supervisor,
                sv_step=sv_step,
                program=program,
                say=_say,
            )

            read_result = read_state.process_turn(
                original_goal=original_goal,
                sv_step=sv_step,
                observation_png=observation.png_bytes,
                bundle=bundle,
                turn_no=turn_no,
                say=_say,
            )
            read_added_content = read_result.added_content
            read_note_hash = read_result.note_hash

            action_decision = action_result.action_decision
            executed = action_result.executed

            # Post-action targeting verify: did the snapped tap land on target?
            # Submit now so it runs concurrently with the settle below; resolved
            # there and stored on the turn for the next turn's off_target check.
            verify_future = submit_target_verify(
                action_decision=action_decision,
                executed=executed,
                sv_step=sv_step,
                observation_png=observation.png_bytes,
                pool=_VERIFY_POOL,
            )

            _stmt_info, _stmt_iid = emit_statement_fields(supervisor)
            turn = record_interactive_turn(
                context=context,
                observation_source=observation.source,
                observation_url=observation_url_for_turn,
                surface_id=(
                    supervisor.surface_id(observation)
                    if callable(getattr(supervisor, "surface_id", None))
                    else ""
                ),
                supervisor_step=sv_step,
                supervisor=supervisor,
                action_decision=action_decision,
                executed=executed,
                action_role=action_result.action_role,
                action_key=action_result.action_key,
                suppressed_reason=action_result.suppressed_reason,
                binding=action_result.binding,
                llm_calls_before=_record_llm_mark,
                tokens_before=_record_token_mark,
                turn_started_at=turn_started_at,
                read_added_content=read_added_content,
                read_note_hash=read_note_hash,
                save_context=_save_ctx,
                silent=silent,
                on_turn=on_turn,
                statement=_stmt_info,
                statement_instance_id=_stmt_iid,
            )

            if not (executed and auto_continue):
                interrupted = _stop_after_esc(turn_no)
                if interrupted is not None:
                    return interrupted

            progress = evaluate_turn_progress(
                sv_step=sv_step,
                executed=executed,
            )
            if progress.stop_reason:
                if progress.stop_message:
                    _say(progress.stop_message)
                return _finish(_orch_result(
                    context,
                    rt.interpreter,
                    progress.stop_reason,
                    current=rt.current,
                ))
            if auto_continue:
                if executed:
                    finalize_auto_continue_turn(
                        turn=turn,
                        action_decision=action_decision,
                        platform=platform,
                        observation_png=observation.png_bytes,
                        verify_future=verify_future,
                        say=_say,
                    )
                _save_ctx()  # 落盘 settle_s（+ target_verify）
                interrupted = _stop_after_esc(turn_no)
                if interrupted is not None:
                    return interrupted
                continue

            try:
                answer = input("继续下一轮？[Enter继续 / q退出] ").strip().lower()
            except EOFError:
                answer = ""
            if answer in {"q", "quit", "exit"}:
                return _finish(_orch_result(
                    context,
                    rt.interpreter,
                    "用户退出 agent-loop",
                    current=rt.current,
                ))



def main() -> None:
    from gui_agent.core.run.cli import main as cli_main

    cli_main(
        run_loop=run_agent_loop,
        policy_builder=build_policy,
        supervisor_builder=build_supervisor,
    )


if __name__ == "__main__":
    main()
