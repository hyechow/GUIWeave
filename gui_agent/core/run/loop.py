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
from gui_agent.core.run.non_interactive import drive_pending_non_ui
from gui_agent.core.orchestrator.list_traversal_runtime import ListTraversalRuntime
from gui_agent.core.orchestrator.engine import (
    is_list_read,
    package_result,
    task_type_for,
    to_milestone,
)
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
MAX_EMPTY_RETURN_RECOVERIES = 3  # 返回字段为空时，最多把当前 UI run 收紧后重新驱动几次

# 动作重试机制暂时关闭：每轮只做一次 action policy 决策和执行。
# MAX_ACTION_RETRIES = 2        # 动作无效时最多重试次数
# ACTION_EFFECT_THRESHOLD = 3.0  # mean_image_diff 低于此值视为动作未生效


def _missing_ui_return_fields(run: object, reads: dict[str, str]) -> list[str]:
    """Return UI-run fields that were declared but not actually read.

    A navigation/action/filter run with ``returns`` is only complete for the
    orchestrator once those fields have values. Empty values mean the milestone
    was accepted too early or on the wrong page, so the plan should not advance
    to later steps that interpolate blanks.
    """
    if run is None or not getattr(run, "returns", None):
        return []
    if is_list_read(run) or getattr(run, "kind", "") in {"read", "data_query"}:
        return []
    missing: list[str] = []
    for field in getattr(run, "returns", []):
        if not str(reads.get(str(field), "")).strip():
            missing.append(str(field))
    return missing


def _tighten_ui_return_run(run: object, missing: list[str], reads: dict[str, str], *, attempt: int) -> object:
    """Make a returning UI run stricter after its completion frame read blanks.

    The decomposer may author a broad success condition such as "page loaded" while
    the run also declares return fields. If the checker accepts the page before
    those fields are visible, continue the same UI milestone with an explicit
    non-empty return-field gate instead of advancing with blanks or waiting as if
    the page were loading.
    """
    if run is None or not hasattr(run, "model_copy"):
        return run
    returns = [str(field) for field in getattr(run, "returns", [])]
    missing_text = "、".join(str(field) for field in missing)
    present = {
        str(field): str(value).strip()
        for field, value in reads.items()
        if str(value).strip()
    }
    present_text = "、".join(f"{field}={value}" for field, value in present.items()) or "无"
    base_success = str(getattr(run, "success_condition", "") or f"完成「{getattr(run, 'name', '当前子目标')}」")
    base_read_spec = str(getattr(run, "read_spec", "") or "")
    recovery = (
        f"返回字段恢复尝试 {attempt}: 当前完成帧未读到所有必需字段。"
        f"已读非空值：{present_text}；缺失字段：{missing_text}。"
        f"只有当这些字段都能从界面明确读取到非空值时才算完成：{'、'.join(returns)}。"
        "如果当前屏幕不可见，不要验收完成；继续执行必要的页面内操作，例如等待、滚动、"
        "打开可见的详情/统计/菜单入口、或使用页面搜索，直到缺失字段的具体值可见。"
    )
    name = str(getattr(run, "name", "当前子目标"))
    return run.model_copy(update={
        "name": f"{name}（继续定位返回字段：{missing_text}）",
        "success_condition": f"{base_success}\n{recovery}",
        "read_spec": f"{base_read_spec}\n{recovery}".strip(),
    })


def _force_interactive_return_recovery(program: object, directive: str) -> object:
    """Convert a mistaken current-frame read into a UI locating run after empty returns.

    A kickback caused by empty UI return fields means the current frame did not
    expose the required values. If the redecomposer responds with a scalar
    ``read`` as the first step, that read can only repeat the same empty frame.
    Treat it as an interactive page-location milestone so the supervisor can
    scroll, expand sections, or navigate within the page before the structured
    return extraction runs.
    """
    if "实际读取结果为空" not in directive or "返回字段" not in directive:
        return program
    if not hasattr(program, "statements") or not hasattr(program, "model_copy"):
        return program

    from gui_agent.core.orchestrator.program import Run

    statements = list(getattr(program, "statements", []) or [])
    if not statements:
        return program
    first = statements[0]
    if (
        not isinstance(first, Run)
        or first.kind != "read"
        or not first.returns
        or first.list_read
    ):
        return program

    fields = "、".join(str(field) for field in first.returns)
    recovery = (
        "上一次已在当前完成帧尝试读取这些返回字段但结果为空。"
        f"本步必须先通过界面定位让字段值可见，字段包括：{fields}。"
        "如果当前屏幕看不到这些值，不要验收完成；继续滚动、展开页面内相关区域、"
        "打开可见的统计/详情入口或使用页面搜索，直到所有字段都有非空可读值。"
    )
    success = str(first.success_condition or f"页面显示可读取的返回字段：{fields}")
    read_spec = str(first.read_spec or "")
    statements[0] = first.model_copy(update={
        "kind": "navigation",
        "success_condition": f"{success}\n{recovery}",
        "read_spec": f"{read_spec}\n{recovery}".strip(),
    })
    return program.model_copy(update={"statements": statements})


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


# Feasibility Guard: how many times a single run may re-decompose after a feasibility kick-back. Bounded
# to avoid an infinite re-plan loop (the same dead-end milestone re-appearing). One is enough to
# swap an infeasible route for the prescribed feasible one; a second kick-back ends the run.
MAX_KICKBACK_REPLANS = 1


def should_kickback_replan(sv_step, program, redecompose, replan_count: int) -> bool:
    """Decide whether a stop step is a Feasibility Guard kick-back to re-decompose (vs a terminal stop).

    True only when: we're in orchestrator mode (program), the supervisor attached a re-plan
    directive (milestone judged infeasible), a redecompose callable is wired, and the per-run
    budget is not yet spent. Otherwise the stop is handled normally (terminal)."""
    return bool(
        program is not None
        and getattr(sv_step, "replan_directive", None)
        and callable(redecompose)
        and replan_count < MAX_KICKBACK_REPLANS
    )


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
    redecompose: object = None,  # callable(directive:str)->Program|None; Feasibility Guard kick-back re-plan. None disables.
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
        _list_traversal_runtime: "ListTraversalRuntime | None" = None

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

        _nonui_failure: "str | None" = None  # last re-plannable non-UI (data_query) failure evidence

        def _ensure_list_traversal_runtime() -> "ListTraversalRuntime | None":
            nonlocal _list_traversal_runtime
            if _cur_run is None or not is_list_read(_cur_run):
                if _list_traversal_runtime is not None:
                    _list_traversal_runtime = None
                if hasattr(supervisor, "note_collection_progress"):
                    supervisor.note_collection_progress("", done=False)
                return None
            var = _cur_run.var or f"m{_run_idx}_read"
            if _list_traversal_runtime is None or _list_traversal_runtime.var != var:
                _list_traversal_runtime = ListTraversalRuntime(
                    var=var,
                    returns=list(_cur_run.returns),
                    read_spec=_cur_run.read_spec or "",
                )
            return _list_traversal_runtime

        def _update_list_traversal_runtime(observation) -> None:
            """Refresh the deterministic list traversal controller for the current frame."""
            runtime = _ensure_list_traversal_runtime()
            if runtime is None or _cur_run is None:
                return

            before = len(runtime.rows)
            decision = runtime.update(observation)
            if hasattr(supervisor, "note_collection_progress"):
                supervisor.note_collection_progress(runtime.prompt_text(), done=runtime.done)
            delta = len(runtime.rows) - before
            delta_text = f", +{delta} 行" if delta else ""
            _say(f"  [Collect] rows={len(runtime.rows)}{delta_text}, next={decision.action}: {decision.reason}")

        def _read_completed_run_returns(run, observation) -> dict[str, str]:
            """Extract a UI run's declared return fields from its completion frame (pure vision:
            structured_read off the screenshot — no host-side URL fetching)."""
            if run is None or not getattr(run, "returns", None):
                return {}
            if is_list_read(run) or getattr(run, "kind", "") in {"read", "data_query"}:
                return {}
            from gui_agent.core.orchestrator.structured_read import structured_read

            returns = list(run.returns)
            read_spec = getattr(run, "read_spec", "") or ""
            reads = structured_read(
                observation.png_bytes,
                returns,
                read_spec=read_spec,
                check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
            )
            _say(f"  [Orchestrator] 动作返回读取 {returns} → {reads}")
            return reads

        def _drive_pending_non_ui(done_observation=None, observation_url: str | None = None) -> "str | None":
            """Drive pending non-UI primitives and sync the local interpreter cursor."""
            nonlocal _cur_run, _run_idx, _notes_mark, _nonui_failure
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
                # Surface drill progress on the HUD: non-UI primitives run inside a hand-off (no
                # top-level `--- Turn N ---`), so the HUD would otherwise freeze through a long drill.
                # Use the interactive-turn count (not a turn_no var that isn't in scope at every call site).
                status=lambda msg: _status(_interactive_turn_count(context), msg),
                done_observation=done_observation,
                observation_url=observation_url,
                # a PROVIDER, not a snapshot: a foreach's into table is populated mid-drain (when the
                # last body return completes), so the data_query must read it fresh — see drive_pending_non_ui.
                materialized_tables=(lambda: _interp.materialized_tables()) if _interp is not None else None,
            )
            _cur_run = result.current_run
            _run_idx = result.run_index
            _notes_mark = result.notes_mark
            _nonui_failure = result.failure_evidence
            return result.reply

        if program is not None:
            from gui_agent.core.orchestrator import Interpreter
            _interp = Interpreter(program)
            _orch_interp = _interp  # _save_ctx now mirrors its run_log (reads) into context
            _orchestrator_reports = list(orchestrator_context_reports or [])
            _orchestrator_metrics = next(
                (
                    report for report in _orchestrator_reports
                    if isinstance(report, dict) and report.get("kind") == "orchestrator_metrics"
                ),
                {},
            )
            # Persist the decomposed program so the report renders decompose as its OWN row
            # (a distinct stage now, not folded into turn 1's supervisor step).
            context.orchestrator = {
                "program": program.model_dump(mode="json"),
                "max_turns": max_turns,
                "context_reports": _orchestrator_reports,
                "timings": dict(_orchestrator_metrics.get("timings") or {}),
                "token_usage": dict(_orchestrator_metrics.get("token_usage") or {}),
                "llm_calls": int(_orchestrator_metrics.get("llm_calls") or 0),
            }
            _gen = _interp.steps()
            try:
                _cur_run = next(_gen)
            except StopIteration as _e:  # program with no run() (just finish / empty)
                return _finish(_orch_result(context, _interp, _e.value or ""))
            _reply = _drive_pending_non_ui()  # leading non-UI step(s) + reseed the first UI run
            if _reply is not None:
                return _finish(_orch_result(context, _interp, _reply))

        _kickback_replans = 0  # Feasibility Guard: re-decompose count this run (bounded by MAX_KICKBACK_REPLANS)
        _empty_return_recoveries: dict[tuple[int, str, tuple[str, ...]], int] = {}

        def _perform_replan(directive: str, observation=None) -> "tuple[bool, str | None]":
            """Re-decompose the REMAINING plan with a kick-back directive + hot-swap the interpreter.
            Returns (handled, reply): (False, None) = not applicable/failed → caller handles normally;
            (True, None) = re-planned & primed → caller should `continue`; (True, reply) = the new
            program ended immediately → caller should _finish with reply. Bounded + guard inside.

            Mid-run state is carried forward, not discarded: the executed milestones + their outcomes
            become the re-decompose's EXPERIENCE, the unexecuted ones its TARGET (summarize_progress),
            the CURRENT observation its page context, and the new interpreter inherits env/run_log so
            already-collected reads still back the final answer (the user's "重编排是有状态记忆的编排")."""
            nonlocal _interp, _orch_interp, _gen, _cur_run, _kickback_replans, _list_traversal_runtime
            if not (program is not None and directive and callable(redecompose)
                    and _kickback_replans < MAX_KICKBACK_REPLANS):
                return (False, None)
            _kickback_replans += 1
            _say(f"\n[Kickback] 重规划 ({_kickback_replans}/{MAX_KICKBACK_REPLANS})：{directive[:120]}")
            _rd_calls0 = get_llm_call_count()
            _rd_tok0 = get_llm_token_usage()
            _rd_t0 = time.perf_counter()
            _rd_reports: list = []  # the re-decompose's LLM call trace → its report 模型调用详情
            from gui_agent.core.orchestrator import Interpreter, summarize_progress
            # Snapshot mid-run state BEFORE swapping: experience (done) + remaining (target) for the
            # re-decompose prompt, and env/run_log to inherit onto the new interpreter.
            _experience, _remaining = summarize_progress(program, _interp.run_log, _cur_run)
            _prev_env = dict(_interp.env)
            _prev_log = list(_interp.run_log)
            if _experience:
                _say(f"  [Kickback] 已执行经验 {len(_prev_log)} 步、剩余目标若干 → 仅重排剩余（带经验+当前页面）")
            try:
                _new = redecompose(
                    directive, _rd_reports,
                    observation=observation,
                    prior_experience=_experience,
                    remaining_plan=_remaining,
                )  # type: ignore[operator]
            except Exception as _exc:  # noqa: BLE001 - a redecompose failure must not crash the run
                _say(f"[Kickback] 重规划失败（{_exc}），按原结果收尾")
                return (False, None)
            if _new is None or not _new.statements:
                return (False, None)
            _new = _force_interactive_return_recovery(_new, directive)
            _interp = Interpreter(_new)
            _interp.env = _prev_env            # carry forward completed reads (finish refs still resolve)
            # Keep prior milestones in the run record / final summary, but DROP the failed record(s):
            # a kickback re-plans *because* a re-plannable step failed, so carrying that ✗ into the new
            # interpreter's run_log would keep `interp.failed` permanently True and force goal_completed
            # =False even after the re-decompose recovers (result.py:72) — the superseded failure must
            # not outvote the successful retry. The full experience (incl. the ✗) was already snapshotted
            # for the redecompose prompt above; if the new plan fails too, its own ✗ records set failed.
            _interp.run_log = [r for r in _prev_log if not r.result.failed]
            _orch_interp = _interp
            # Keep context.orchestrator["program"] = the ORIGINAL (#0); record each kick-back
            # re-decompose as its own entry (directive + new program + the turn that triggered it) so
            # the report renders it as a SEPARATE card (#0↻N), not by overwriting the original plan.
            _prior = context.orchestrator or {}
            _redecomps = list(_prior.get("redecomposes") or [])
            _redecomps.append({
                "kickback_n": _kickback_replans,
                "directive": directive,
                "at_turn": len(context.turns),
                "program": _new.model_dump(mode="json"),
                # the re-decompose's own LLM call metrics, so its report card shows 模型调用详情
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
                "redecomposes": _redecomps,
                "replanned_from_kickback": _kickback_replans,
            }
            _gen = _interp.steps()
            _list_traversal_runtime = None
            try:
                _cur_run = next(_gen)
            except StopIteration as _e:
                return (True, _e.value or "")
            _reply2 = _drive_pending_non_ui()  # reseed first run of the new program
            return (True, _reply2) if _reply2 is not None else (True, None)

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
            _did_kickback_replan = False
            _did_return_recovery = False
            _terminal_verdict_recorded = False
            # Same-turn hand-offs call supervisor.step() multiple times; each step() clears its own
            # _timings, so the completion check that detected the prior milestone done would be lost
            # from this turn's breakdown. Carry each handed-off step's timings here and merge them
            # back after the loop, so the report shows the checker call that ran on the hand-off.
            _carry = SupervisorTimingCarry()
            while True:
                _update_list_traversal_runtime(observation)
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
                _rows = (
                    list(_list_traversal_runtime.rows)
                    if _cur_run is not None and is_list_read(_cur_run) and _list_traversal_runtime is not None
                    else []
                )
                _reads = _read_completed_run_returns(_cur_run, observation)
                _missing_returns = _missing_ui_return_fields(_cur_run, _reads)
                if _missing_returns:
                    _directive = (
                        "上一子目标被验收为完成，但它声明必须读取返回字段 "
                        f"{_missing_returns}，实际读取结果为空：{_reads}。"
                        "这说明验收过早或页面不对。不要推进到会使用空值的后续步骤；"
                        "请从当前页面继续或重规划，先真正定位目标数据并读取非空返回值。"
                    )
                    _handled, _r = _perform_replan(_directive, observation)
                    if _handled and _r is not None:
                        _orch_reply = _r
                    elif _handled:
                        _did_kickback_replan = True
                    else:
                        _recovery_key = (
                            _run_idx,
                            str(getattr(_cur_run, "var", "") or getattr(_cur_run, "name", "")),
                            tuple(str(field) for field in getattr(_cur_run, "returns", [])),
                        )
                        _attempt = _empty_return_recoveries.get(_recovery_key, 0) + 1
                        if _attempt <= MAX_EMPTY_RETURN_RECOVERIES:
                            _empty_return_recoveries[_recovery_key] = _attempt
                            _cur_run = _tighten_ui_return_run(
                                _cur_run,
                                _missing_returns,
                                _reads,
                                attempt=_attempt,
                            )
                            supervisor.reseed(
                                to_milestone(_cur_run, _run_idx),
                                task_type=task_type_for(_cur_run),
                                fresh_advance=False,
                            )
                            _say(
                                "  [Orchestrator] 返回字段为空，继续定位"
                                f"（{_attempt}/{MAX_EMPTY_RETURN_RECOVERIES}）："
                                + "、".join(_missing_returns)
                            )
                            _did_return_recovery = True
                        else:
                            _say(
                                "  [Orchestrator] 返回字段持续为空，停止推进："
                                + "、".join(_missing_returns)
                            )
                            _hand = package_result(
                                _cur_run,
                                completed=False,
                                summary="必需返回字段为空：" + "、".join(_missing_returns),
                                notes=context.content_notes[_notes_mark:],
                                reads=_reads,
                            )
                            try:
                                _cur_run = _gen.send(_hand)
                            except StopIteration as _e:
                                _orch_reply = _e.value or ""
                            else:
                                _did_return_recovery = True
                    break
                _hand = package_result(
                    _cur_run, completed=True, summary=sv_step.summary or "完成",
                    notes=context.content_notes[_notes_mark:],
                    reads=_reads,
                    rows=_rows,
                )
                if _cur_run is not None and is_list_read(_cur_run):
                    _list_traversal_runtime = None
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
                        observation_url=f"screenshot_turn_{turn_no}.png",
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
                        observation_url=f"screenshot_turn_{turn_no}.png",
                        supervisor_step=sv_step,
                        supervisor=supervisor,
                        llm_calls=get_llm_call_count() - llm_calls_before,
                        input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                        output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                    ))
                # Sync the last milestone's done verdict into context (no later turn body runs).
                sync_milestone_states(supervisor, context)
                # Feasibility Guard non-UI kick-back: the program ended because a data_query failed with a
                # re-plannable data-source issue (source empty / mismatched with the task). Re-decompose
                # with that evidence as the directive instead of finishing on the failure.
                if _nonui_failure:
                    _directive = (
                        "上一份计划在 data_query 步失败：" + _nonui_failure
                        + "\n这说明取数路线不对（数据源为空或与任务口径不一致）。请改用能真正拿到目标数据的"
                          "路线重新规划，不要重复上面失败的取数方式。"
                    )
                    _handled, _r = _perform_replan(_directive, observation)
                    if _handled and _r is not None:
                        return _finish(_orch_result(context, _interp, _r))
                    if _handled:
                        _nonui_failure = None
                        continue
                return _finish(_orch_result(context, _interp, _orch_reply))

            if _did_kickback_replan or _did_return_recovery:
                continue

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

            if _cur_run is not None and is_list_read(_cur_run):
                read_added_content = False
                read_note_hash = None
            else:
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
                observation_url=f"screenshot_turn_{turn_no}.png",
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

            note_executed_action = getattr(supervisor, "note_executed_action", None)
            if callable(note_executed_action):
                note_executed_action(
                    index=turn.index,
                    observation=observation,
                    supervisor_step=sv_step,
                    action_decision=action_decision,
                    executed=executed,
                )

            # Feasibility Guard kick-back: the supervisor judged the milestone INFEASIBLE and attached a
            # re-plan directive. Re-decompose the goal with that directive and hot-swap the
            # interpreter, instead of failing the run. Bounded (MAX_KICKBACK_REPLANS); any failure
            # (redecompose raises / empty program) falls through to the normal terminal handling.
            if should_kickback_replan(sv_step, program, redecompose, _kickback_replans):
                _say("\n[Kickback] milestone 判定不可行 → 重规划")
                _handled, _reply = _perform_replan(sv_step.replan_directive or "", observation)
                if _handled and _reply is not None:
                    return _finish(_orch_result(context, _interp, _reply))
                if _handled:
                    continue  # resume the loop on the re-decomposed program

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
