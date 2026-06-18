"""Agent loop implementation for policy experiments."""

from __future__ import annotations

import sys
import time
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv()

from llm.structured import get_llm_call_count, get_llm_token_usage
from gui_agent.core.run.content import ReadState, ensure_note_hashes as _ensure_note_hashes
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.llm.reader import ContentReader
from gui_agent.core.run.context import (
    load_context as _load_context,
    save_context as _save_context,
)
from gui_agent.core.run.result import (
    make_result as _make_result,
    orchestration_result as _orch_result,
)
from gui_agent.core.run.action_exec import (
    ActionExecutionState,
    finalize_auto_continue_turn,
    submit_target_verify,
)
from gui_agent.core.run.flow import (
    evaluate_turn_progress,
    finish_terminal_step,
    handle_loading_frame,
)
from gui_agent.core.run.non_ui import drive_pending_non_ui
from gui_agent.core.run.turns import (
    SupervisorTimingCarry,
    interactive_turn_count as _interactive_turn_count,
    make_verdict_turn,
    record_interactive_turn,
    sync_turn_metadata,
)
from gui_agent.core.supervisor.base import SupervisorPolicy
from gui_agent.core.llm.temporal import resolve_temporal_expressions
from gui_agent.core.policies.base import ActionPolicy
from gui_agent.core.schemas import PolicyContext
from gui_agent.core.run.state import (
    sync_context_run_state,
    sync_milestone_states,
)

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

# 动作重试机制暂时关闭：每轮只做一次 action policy 决策和执行。
# MAX_ACTION_RETRIES = 2        # 动作无效时最多重试次数
# ACTION_EFFECT_THRESHOLD = 3.0  # mean_image_diff 低于此值视为动作未生效


# Post-action targeting verify runs in this 1-worker pool so it overlaps the
# settle wait (near-zero added latency). Daemon threads; finishes at process exit.
_VERIFY_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verify")

# YOLO detection + OCR run here, concurrent with the supervisor decide, so the
# snap has its boxes/text ready by execute time (~0.4s off the critical path).
_PREP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prep")

# Stitched-chunk reader (~1.8s LLM read) runs here so it overlaps the action +
# settle + next turn's loop_check. Result is drained at the next turn's read
# block (read_added_content is set inline from the cheap stitch feed, so the
# supervisor's boundary check never waits on the reader). 1 worker keeps
# content_notes ordering. Stitch feed (robust_shift, ~25ms) stays inline: it
# produces read_added_content which the turn record needs synchronously.
_READER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reader")


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
    program: "Program | None" = None,  # DSL program (orchestrator mode); None = DAG path (unchanged)
    orchestrator_context_reports: list[dict] | None = None,
    stop_requested: object = None,  # callable() -> bool; true means stop after current turn settles
    platform: object = None,  # already-open session (runner pre-opens it so router/decompose can see the current front-tab url/title; see cli.py); None → open here (chat path, unchanged)
    headless: bool = False,  # suppress the action visualizer (cursor/overlay) on every platform; HUD is gated by the caller
) -> dict:
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
    _ensure_note_hashes(context)
    if knowledge is not None:
        context.knowledge = knowledge

    _orch_interp = None  # set in orchestrator mode; _save_ctx mirrors its live run_log into context

    def _save_ctx() -> None:
        """Persist context, stamping the run's wall-clock elapsed so far — so the final file
        carries the true end-to-end time (LLM + settle + perception/execution/overhead), not
        just the sum of LLM-module timings."""
        context.wall_clock_s = time.perf_counter() - _run_started
        # Orchestrator mode: mirror the interpreter's live run_log (each completed run + its
        # structured reads) into context so the report shows WHAT each read captured and can
        # resolve {var[field]} action targets — not just the static program structure. A pure
        # read has no turn/milestone (it reads the verdict frame), so this is the ONLY place its
        # result reaches the report. Refreshed every save → an interrupted run still shows partial
        # reads. context.orchestrator otherwise holds {"program": ...} (set when the program lands).
        if _orch_interp is not None and isinstance(context.orchestrator, dict):
            context.orchestrator["run_log"] = [
                r.model_dump(mode="json") for r in _orch_interp.run_log
            ]
        sync_milestone_states(supervisor, context)
        _save_context(context_path, context)

    def _finish(result: dict) -> dict:
        sync_context_run_state(context, result)
        _save_ctx()
        return result

    _save_ctx()
    _say(f"Goal    : {context.goal}")
    _say(f"Turns   : {len(context.turns)}")
    # Pin the task goal as a persistent HUD header (above the live turn status), so
    # the floating panel over the browser/mirror shows WHAT the agent is doing.
    if hud is not None and hasattr(hud, "set_goal"):
        hud.set_goal(context.goal)

    action_state = ActionExecutionState()
    original_goal = context.goal
    noop_count = 0
    loading_streak = 0
    prev_milestone_id: str | None = None

    # The platform bundle is the single seam through which the agent loop obtains
    # the session, executor, perception and scroll/stitch helpers — no adapter
    # class is referenced directly here.
    bundle = build_platform(backend=backend)
    reader = ContentReader(prepare_vision_prompt_png=bundle.prepare_vision_prompt_png)
    read_state = ReadState(context=context, reader=reader, pool=_READER_POOL)
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
            return _finish(_make_result(context, f"环境检查未通过：{setup.summary}"))
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
        # the just-connected session — e.g. the WebArena entry injects auth cookies,
        # starts HAR capture and navigates to the task start_url. Runs on the neutral
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

        # ── DSL orchestrator mode ──────────────────────────────────────────────────
        # The interpreter (not the supervisor's walker) sequences milestones. Seed the
        # supervisor with the first run()'s milestone; the loop drives it, and the stop
        # block asks the interpreter for the next on milestone-done. program=None → the
        # DAG path is untouched.
        _interp = None
        _gen = None
        _cur_run = None
        _run_idx = 0
        _notes_mark = 0

        def _stop_after_esc(turn_no: int) -> dict | None:
            if not _stop_requested():
                return None
            read_state.drain_pending(say=_say)
            read_state.flush(turn_no=turn_no, say=_say)
            _say("\n收到 ESC：当前 turn 已收尾，agent-loop 安全停止")
            reason = "用户按 ESC 中止 agent-loop"
            if program is not None:
                return _finish(_orch_result(context, _interp, f"{reason}（任务未完成）", current=_cur_run))
            return _finish(_make_result(context, reason))

        def _drive_pending_non_ui(done_observation=None, observation_url: str | None = None) -> "str | None":
            """Drive pending non-UI primitives and sync the local interpreter cursor."""
            nonlocal _cur_run, _run_idx, _notes_mark
            result = drive_pending_non_ui(
                current_run=_cur_run,
                run_index=_run_idx,
                notes_mark=_notes_mark,
                interpreter_steps=_gen,
                bundle=bundle,
                platform=platform,
                log_dir=log_dir,
                supervisor=supervisor,
                context=context,
                save_context=_save_ctx,
                say=_say,
                done_observation=done_observation,
                observation_url=observation_url,
            )
            _cur_run = result.current_run
            _run_idx = result.run_index
            _notes_mark = result.notes_mark
            return result.reply

        if program is not None:
            from gui_agent.core.orchestrator import Interpreter
            _interp = Interpreter(program)
            _orch_interp = _interp  # _save_ctx now mirrors its run_log (reads) into context
            # Persist the decomposed program so the report renders decompose as its OWN row
            # (a distinct stage now, not folded into turn 1's supervisor step).
            context.orchestrator = {
                "program": program.model_dump(mode="json"),
                "max_turns": max_turns,
                "context_reports": list(orchestrator_context_reports or []),
            }
            _gen = _interp.steps()
            try:
                _cur_run = next(_gen)
            except StopIteration as _e:  # program with no run() (just finish / empty)
                return _finish(_orch_result(context, _interp, _e.value or ""))
            _reply = _drive_pending_non_ui()  # leading non-UI step(s) + reseed the first UI run
            if _reply is not None:
                return _finish(_orch_result(context, _interp, _reply))

        while True:
            interrupted = _stop_after_esc(_interactive_turn_count(context))
            if interrupted is not None:
                return interrupted

            turn_no = len(context.turns) + 1
            if _interactive_turn_count(context) + 1 > max_turns:
                read_state.drain_pending(say=_say)
                read_state.flush(turn_no=turn_no - 1, say=_say)
                _say(f"\n达到最大轮数 {max_turns}，agent-loop 停止")
                if program is not None:  # orchestrator: summarize the whole program so far
                    return _finish(_orch_result(context, _interp, f"达到最大轮数 {max_turns}（任务未完成）", current=_cur_run))
                return _finish(_make_result(context, f"达到最大轮数 {max_turns}"))

            turn_started_at = time.perf_counter()
            llm_calls_before = get_llm_call_count()
            tokens_before = get_llm_token_usage()

            _say("\n" + TURN_HEADER.format(turn_no=turn_no))

            # Observe a fresh frame each turn. A milestone hand-off (the verdict-frame carry-
            # forward: deciding the next milestone on the SAME frame the prior one was accepted
            # on, preserving transient hints + saving a screenshot) is now done WITHIN the turn
            # by the decision-phase loop below — it no longer crosses a turn boundary.
            _status(turn_no, "截图分析中…")
            perception = bundle.make_perception(platform, log_dir / f"screenshot_turn_{turn_no}.png")
            observation = perception.observe()
            # YOLO + OCR run in the background, overlapping the decide below;
            # awaited just before execute (snap) so they add ~no latency.
            prep_future = _PREP_POOL.submit(executor.prepare_frame, observation.png_bytes)

            # ── Decision phase (orchestrator hand-off merge, DAG _advance parity) ────────
            # Decide for the current milestone on THIS frame. In orchestrator mode a milestone
            # that COMPLETES here is a hand-off, not a turn end: package it, advance the
            # interpreter (driving any read runs off this verdict frame), reseed the next
            # milestone, and re-decide on the SAME frame — so a pure milestone hand-off never
            # costs its own action-less turn (the old behavior spent one). Only an action, a
            # DAG-mode completion, or a stop ends the turn. The next milestone's nav skip-check
            # is set by the reseed inside _drive_pending_non_ui (fresh_advance). Behaviorally the
            # next milestone is decided on the exact frame the prior one was accepted on — same
            # as the verdict-frame carry-forward, just merged into this turn instead of the next.
            _orch_reply: "str | None" = None    # set if the program ended during a hand-off
            _did_loading = False
            _terminal_verdict_recorded = False
            # Same-turn hand-offs call supervisor.step() multiple times; each step() clears its own
            # _timings, so the completion check that detected the prior milestone done would be lost
            # from this turn's breakdown. Carry each handed-off step's timings here and merge them
            # back after the loop, so the report shows the checker call that ran on the hand-off.
            _carry = SupervisorTimingCarry()
            while True:
                _status(turn_no, f"使用 {supervisor.name} supervisor 决策中…")
                _say("监督决策中...")
                sv_step = supervisor.step(observation, context.goal, context.turns)
                _say(f"监督者: {sv_step.summary}")
                _status(turn_no, sv_step.summary)

                if sv_step.is_loading:
                    _did_loading = True
                    break
                if program is None or not sv_step.goal_completed:
                    break  # actionable / in_progress / DAG-completion / stop → run the turn body

                # Orchestrator milestone completed → hand off to the next milestone, same frame.
                _done_name = _cur_run.name
                read_state.drain_pending(say=_say)
                read_state.flush(turn_no=turn_no, say=_say)
                from gui_agent.core.orchestrator.engine import package_result
                _hand = package_result(
                    _cur_run, completed=True, summary=sv_step.summary or "完成",
                    notes=context.content_notes[_notes_mark:],
                )
                try:
                    _cur_run = _gen.send(_hand)
                except StopIteration as _e:          # program finished (finish / off end)
                    _orch_reply = _e.value or ""
                    break
                _run_idx += 1
                if _cur_run is not None and _cur_run.kind in {"read", "data_query"}:
                    context.turns.append(make_verdict_turn(
                        index=len(context.turns) + 1,
                        observation_source=observation.source,
                        supervisor_step=sv_step,
                        supervisor=supervisor,
                        llm_calls=get_llm_call_count() - llm_calls_before,
                        input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                        output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                    ))
                    _terminal_verdict_recorded = True
                # non-UI steps consume THIS verdict observation; reseed the next UI milestone.
                _reply = _drive_pending_non_ui(
                    done_observation=observation,
                    observation_url=f"screenshot_turn_{turn_no}.png",
                )
                if _reply is not None:
                    _orch_reply = _reply
                    break
                # Carry this (non-final) step's timings/tokens before the next step() clears them.
                _carry.collect(supervisor)
                _say(f"  [Orchestrator] 子目标「{_done_name}」完成 → 下一子任务："
                     f"{_cur_run.name}（同一验收帧上决策，不另起 turn）")
                # loop: re-decide the freshly-reseeded milestone on the same observation.

            # Merge the carried hand-off step timings into the supervisor's (final step's) timings,
            # so this turn's breakdown includes the checker call that ran on the hand-off. Carried
            # (earlier-step) keys render first; later writes (e.g. action_policy) still append after.
            _carry.merge_into(supervisor)

            if _orch_reply is not None:
                read_state.drain_pending(say=_say)
                # The program ended on this hand-off (last actionable milestone done → read /
                # finish). The merge only DROPS a verdict turn when it folds into a FOLLOWING
                # action; the terminal milestone has none, so record its verdict turn here —
                # otherwise the report is missing it and the verdict screenshot (already written
                # at observe) is orphaned. sv_step is the completed milestone's done verdict.
                if not _terminal_verdict_recorded:
                    context.turns.append(make_verdict_turn(
                        index=len(context.turns) + 1,
                        observation_source=observation.source,
                        supervisor_step=sv_step,
                        supervisor=supervisor,
                        llm_calls=get_llm_call_count() - llm_calls_before,
                        input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                        output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                    ))
                # Sync the last milestone's done verdict into context (no later turn body runs).
                sync_milestone_states(supervisor, context)
                return _finish(_orch_result(context, _interp, _orch_reply))

            # 页面未稳定（白屏/加载中）：等待并重新观察，本帧不写入 context.turns、不消耗
            # max_turns、不累加 noop_count。加载是 App 渲染延迟、不是 agent 的一步操作。
            # 连续加载设上限，防页面永挂导致死循环（旧行为：loading 帧既占轮数，又会因
            # should_act=False 累加 noop_count，连续 3 帧就误判"连续无动作"终止 agent）。
            if _did_loading:
                loading = handle_loading_frame(
                    loading_streak=loading_streak, max_loading_frames=MAX_LOADING_FRAMES,
                    wait_s=LOADING_WAIT_S, turn_no=turn_no, program=program,
                    current_run=_cur_run, context=context, interpreter=_interp,
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

            action_result = action_state.run(
                sv_step=sv_step,
                observation=observation,
                action_policy=action_policy,
                supervisor=supervisor,
                executor=executor,
                bundle=bundle,
                platform=platform,
                prep_future=prep_future,
                log_dir=log_dir,
                turn_no=turn_no,
                flash=_flash,
                status=_status,
                say=_say,
                stop_requested=_stop_requested,
            )
            action_decision = action_result.action_decision
            executed = action_result.executed
            probe_failed = action_result.probe_failed
            branch_settle_s = action_result.branch_settle_s

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

            turn = record_interactive_turn(
                context=context,
                observation_source=observation.source,
                supervisor_step=sv_step,
                supervisor=supervisor,
                action_decision=action_decision,
                executed=executed,
                llm_calls_before=llm_calls_before,
                tokens_before=tokens_before,
                turn_started_at=turn_started_at,
                read_added_content=read_added_content,
                read_note_hash=read_note_hash,
                save_context=_save_ctx,
                silent=silent,
                on_turn=on_turn,
            )

            if sv_step.stop or sv_step.goal_completed:
                return finish_terminal_step(
                    sv_step=sv_step,
                    read_state=read_state,
                    turn_no=turn_no,
                    program=program,
                    current_run=_cur_run,
                    interpreter_steps=_gen,
                    interpreter=_interp,
                    context=context,
                    notes_mark=_notes_mark,
                    finish=_finish,
                    say=_say,
                )

            if not (executed and auto_continue):
                interrupted = _stop_after_esc(turn_no)
                if interrupted is not None:
                    return interrupted

            progress = evaluate_turn_progress(
                noop_count=noop_count,
                prev_milestone_id=prev_milestone_id,
                sv_step=sv_step,
                executed=executed,
                action_decision=action_decision,
                probe_failed=probe_failed,
            )
            noop_count = progress.noop_count
            prev_milestone_id = progress.prev_milestone_id
            if progress.stop_reason:
                if progress.stop_message:
                    _say(progress.stop_message)
                return _finish(_make_result(context, progress.stop_reason))
            if progress.message:
                _say(progress.message)
            if progress.continue_loop:
                continue

            if auto_continue:
                finalize_auto_continue_turn(
                    turn=turn,
                    branch_settle_s=branch_settle_s,
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
                return _finish(_make_result(context, "用户退出 agent-loop"))



def main() -> None:
    from gui_agent.core.run.cli import main as cli_main

    cli_main(
        run_loop=run_agent_loop,
        policy_builder=build_policy,
        supervisor_builder=build_supervisor,
    )


if __name__ == "__main__":
    main()
