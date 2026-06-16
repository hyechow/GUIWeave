"""Agent loop implementation for policy experiments."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv()

from llm.structured import get_llm_call_count, get_llm_token_usage
from gui_agent.core.run.content import (
    STITCH_OVERLAP_PX,
    ensure_note_hashes as _ensure_note_hashes,
    flush_and_read as _flush_and_read,
    note_hash as _note_hash,
    store_chunk_note as _store_chunk_note,
)
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.vision.frame_analysis import STABLE_MEAN_THR, frame_changed, frame_diff
from gui_agent.core.llm.reader import ContentReader, build_reader_instruction
from gui_agent.core.run.context import (
    extract_checker as _extract_checker,
    extract_plan as _extract_plan,
    extract_replan as _extract_replan,
    load_context as _load_context,
    save_context as _save_context,
)
from gui_agent.core.run.result import (
    make_result as _make_result,
    orchestration_result as _orch_result,
    print_timings as _print_timings,
    print_turn_stats as _print_turn_stats,
)
from gui_agent.core.supervisor.base import SupervisorPolicy
from gui_agent.core.llm.temporal import resolve_temporal_expressions
from gui_agent.core.policies.base import ActionPolicy
from gui_agent.core.vision.target_verify import verify_target
from gui_agent.core.schemas import (
    ActionDecision,
    PolicyContext,
    PolicyTurn,
    action_label,
)
from gui_agent.core.run.state import (
    sync_context_run_state,
    sync_milestone_states,
)
from gui_agent.core.vision.visualize import print_decision

if TYPE_CHECKING:
    # Adapter types used only in annotations. With `from __future__ import
    # annotations` these stay lazy strings, so importing runner pulls in no
    # adapter at module top.
    from gui_agent.core.ui.hud import AgentHUD
    from gui_agent.adapters.iphone.scroll_probe import ScrollProfile
    from gui_agent.adapters.iphone.stitch import StitchAccumulator
    from gui_agent.core.runtime.contracts import PerceptionSession

TURN_HEADER = "\033[1;36m--- Turn {turn_no} ---\033[0m"
# 动作后自适应等待：轮询截图，等到屏幕「相对动作前帧变过、且相对上一帧停稳」再进入
# 下一轮决策。首帧间隔较长（SETTLE_FIRST_S），让 App 启动 zoom、页面横滑等转场动画
# 先跑完，避免在动画中途采样导致相邻两帧「碰巧相似」被误判停稳；之后用 SETTLE_UNIT_S
# 细粒度轮询。tap 冷启动会续等到效果出现，scroll 续等惯性停止，no-op 等到上限兜底。
SETTLE_FIRST_S = 1.0      # 首帧等待：覆盖大多数转场动画（zoom/横滑 ~0.3-0.5s）
SETTLE_UNIT_S = 0.5       # 后续轮询间隔
SETTLE_MAX_UNITS = 6
# 帧级视觉判定与其阈值（frame_diff 稳定性 / frame_changed 生效 / STABLE_MEAN_THR）都在
# core/frame_analysis.py，不再散落在 runner。
# drag/scroll 是页内操作，不触发页面加载/转场，但改动常局限一小块（如 picker 滚轮带），
# 全屏均值差测不出「changed」（实测 picker 拖动仅 0.1-0.25 << CHANGE_THR=8.0），用原
# 「变过且停稳」逻辑会每次顶满上限白等 ~4s。但也不能盲等固定时长：fling 惯性时长不定
# （轻拨 ~0.3s，重拨 1-2s），固定值太短会在滚轮没停稳时就截图，checker 读到滞后的标签
# 值（轮子已滑过、标签未更新），导致位移误算、来回震荡。故对 drag/scroll 只判「停稳」
# （相邻帧不再变化）、不判「changed」：轻拨很快返回，重拨等到真停，且保证读数准。
SETTLE_GESTURE_FIRST_S = 0.3  # drag/scroll 首帧：让惯性先跑起来，避免抬手瞬间误判停稳

# 页面未稳定（白屏/加载中）的等待帧：不计入 max_turns、不累加 noop，只重新观察。
# 加载是 App 渲染延迟、不是 agent 的一步操作，不该消耗轮数预算；但要设上限防页面永挂死循环。
LOADING_WAIT_S = 0.6          # 每个加载帧重新观察前的等待，给页面渲染时间
MAX_LOADING_FRAMES = 12       # 连续加载帧上限，超过即判页面永挂、停止

# 动作重试机制暂时关闭：每轮只做一次 action policy 决策和执行。
# MAX_ACTION_RETRIES = 2        # 动作无效时最多重试次数
# ACTION_EFFECT_THRESHOLD = 3.0  # mean_image_diff 低于此值视为动作未生效


def _settle_after_action(
    phone: "PerceptionSession", pre_frame: bytes | None, action_type: str | None = None,
    focus_y: float | None = None, center: tuple[float, float] | None = None,
) -> tuple[float, bool]:
    """等到屏幕相对动作前帧「变过且停稳」，或达到上限。返回 (等待秒数, no_effect)。

    必须对照动作前帧：否则冷启动那 ~1s 静止旧画面会被误判为已就绪。

    no_effect=True 仅用于 tap 类动作：跑满上限且**全程相对动作前帧从未 changed**——
    即这一击对屏幕零效果（如重点已高亮的 tab、点到惰性元素）。drag/scroll 改动可能很
    小、不押 changed，故恒为 False；无动作前帧时也无从判断，False。

    drag/scroll 只判「停稳」（相邻帧不再变化）、不判「changed」：picker 改动小测不出
    changed 会顶满上限，而固定盲等又会在 fling 没停时截图导致读数滞后、来回震荡。

    浏览器优先走 CDP settle（phone.wait_settled：readyState + DOM 变更静默 + 网络静默）——
    它读页面真实状态、无视 canvas/rAF 动效，远早于视觉返回。手势（drag/scroll）仍走视觉：
    平滑滚动是纯视觉转场、不产生 DOM/网络信号，DOM 静默会在滚动途中就触发。CDP 异常则回退视觉。
    """
    if action_type not in ("drag", "scroll"):
        _cdp_settle = getattr(phone, "wait_settled", None)
        if _cdp_settle is not None:
            try:
                return _cdp_settle(action_type)
            except Exception as e:
                print(f"  [Settle] CDP settle 异常，回退视觉: {e}")
    t0 = time.perf_counter()
    if action_type in ("drag", "scroll"):
        prev: bytes | None = None
        for i in range(1, SETTLE_MAX_UNITS + 1):
            time.sleep(SETTLE_GESTURE_FIRST_S if i == 1 else SETTLE_UNIT_S)
            try:
                cur = phone.screenshot()
            except Exception:
                dur = time.perf_counter() - t0
                print(f"  [Settle] {dur:.1f}s ({i} 轮，截图异常提前返回)")
                return dur, False
            if prev is not None and frame_diff(prev, cur) < STABLE_MEAN_THR:
                dur = time.perf_counter() - t0
                print(f"  [Settle] {dur:.1f}s ({i} 轮，停稳: {action_type})")
                return dur, False
            prev = cur
        dur = time.perf_counter() - t0
        print(f"  [Settle] {dur:.1f}s ({SETTLE_MAX_UNITS} 轮，达上限: {action_type})")
        return dur, False
    if pre_frame is None:
        time.sleep(SETTLE_FIRST_S)
        dur = time.perf_counter() - t0
        print(f"  [Settle] {dur:.1f}s (无动作前帧)")
        return dur, False
    prev: bytes | None = None
    ever_changed = False
    # Browser tab-switch detector (optional: only present on browser session).
    _pop_tab = getattr(phone, "pop_tab_switched", None)
    for i in range(1, SETTLE_MAX_UNITS + 1):
        # 首帧等久一点让转场动画跑完，再用细粒度轮询。
        time.sleep(SETTLE_FIRST_S if i == 1 else SETTLE_UNIT_S)
        try:
            cur = phone.screenshot()
        except Exception:
            dur = time.perf_counter() - t0
            print(f"  [Settle] {dur:.1f}s ({i} 轮，截图异常提前返回)")
            return dur, False
        # Browser: a tab switch is always "effect" even when pixel diff < threshold
        # (new tab may look visually similar to the old one at 160x320 resolution).
        tab_just_switched = bool(_pop_tab and _pop_tab())
        if tab_just_switched:
            ever_changed = True
            print(f"  [Settle] {time.perf_counter() - t0:.1f}s ({i} 轮，tab切换→有效果)")
        # 「是否生效」用结构+颜色信号判（见 frame_analysis.frame_changed），不靠全屏灰度均值——
        # 后者会把 tab 切换这种明显变页(ssim_dist 0.167、但 mean 仅 6.1)误判成零效果。type 传
        # focus_y 只看输入行带；tap 传 center 只看点击点周围 box——否则菜单展开/下拉这种局部改动
        # 会被整帧稀释成「零效果」(实测点订单菜单整帧 ssim_dist 0.026<0.08，点击点 box 内达 0.29)。
        changed = frame_changed(pre_frame, cur, focus_y, center=center)
        ever_changed = ever_changed or changed
        stable = prev is not None and frame_diff(prev, cur, focus_y) < STABLE_MEAN_THR
        if (changed or tab_just_switched) and stable:
            dur = time.perf_counter() - t0
            print(f"  [Settle] {dur:.1f}s ({i} 轮，变过且停稳)")
            return dur, False
        prev = cur
    dur = time.perf_counter() - t0
    no_effect = not ever_changed
    tag = "达上限·零效果" if no_effect else "达上限"
    print(f"  [Settle] {dur:.1f}s ({SETTLE_MAX_UNITS} 轮，{tag})")
    return dur, no_effect


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


def _snapped_point(action_decision: ActionDecision | None) -> tuple[float, float] | None:
    """The actual tap location (snapped if snapping fired, else raw) for tap/click."""
    if action_decision is None:
        return None
    a = action_decision.action
    if a.action_type not in ("tap", "click") or a.x is None or a.y is None:
        return None
    snap = a.snap
    if snap and snap.get("snapped"):
        sx, sy = snap["snapped"]
        return float(sx), float(sy)
    return float(a.x), float(a.y)


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
    on_session_open: object = None,  # callable(phone) run once after session open, before the loop
    knowledge: dict | None = None,  # injected app-knowledge summary {app_name, nav_chars, ...}; None if no match
    program: "Program | None" = None,  # DSL program (orchestrator mode); None = DAG path (unchanged)
    stop_requested: object = None,  # callable() -> bool; true means stop after current turn settles
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

    reader = ContentReader()
    original_goal = context.goal
    noop_count = 0
    loading_streak = 0
    prev_milestone_id: str | None = None
    scroll_profiles: dict[str, ScrollProfile] = {}
    scroll_probe_failures: dict[str, str] = {}
    # 拼接采集状态：同一时刻只有一个采集子目标，故单个「当前累积器」即可。
    stitch_acc: StitchAccumulator | None = None
    stitch_acc_mid: str | None = None
    stitch_acc_instr: str = ""
    stitch_acc_sv = None
    # 已采集行的精确哈希集合（跨 chunk + resume 去重）。resume 时从既有内容按行重建。
    seen_rows: set[str] = set()
    for _n in context.content_notes:
        for _ln in _n.splitlines():
            _s = _ln.strip()
            if _s:
                seen_rows.add(_note_hash(_s))

    # 后台 reader 读取的待入库结果：(future→list[note], turn_no, sv_step)。在下一轮 read 块
    # 开头 drain（此时它早已与动作/settle/下一轮 loop_check 重叠完成）。文本去重+入库都在主
    # 线程做（仅 reader.read 的网络 I/O 在后台），避免 content_notes/seen_rows 并发写。
    pending_read: tuple[Future, int, object] | None = None

    def _drain_pending_read() -> None:
        nonlocal pending_read
        if pending_read is None:
            return
        fut, tno, sv = pending_read
        pending_read = None
        for note in fut.result():
            if _store_chunk_note(note, context, seen_rows, turn_no=tno, sv_step=sv):
                _say(f"内容摘要(块): {context.content_notes[-1][:80]}...")

    # The platform bundle is the single seam through which the agent loop obtains
    # the session, executor, perception and scroll/stitch helpers — no adapter
    # class is referenced directly here.
    bundle = build_platform(backend=backend)
    # Record the platform on the context so the log and the HTML report can label
    # the run (iphone vs browser). Set here so both the runner and chat (which both
    # call run_agent_loop) persist it.
    if context.platform != bundle.platform:
        context.platform = bundle.platform
        _save_ctx()

    # Pre-session environment check (mirror open / CDP up / adb+ADBKeyboard ready). Runs
    # ONCE here, before the session opens, so a blocking precondition aborts with a clear
    # message instead of crashing deep inside connect. Any one-time setup a platform needs
    # (android switching the IME to ADBKeyboard) happens inside the check.
    setup = bundle.setup_check()
    for _line in setup.lines:
        _say(_line)
    if not setup.ok:
        _say(f"\n环境检查未通过：{setup.summary}")
        return _finish(_make_result(context, f"环境检查未通过：{setup.summary}"))

    with bundle.open_session() as phone:
        executor = bundle.make_executor(phone)
        # Optional action visualizer (cursor/overlay). None when the platform has
        # none (iphone today); show_action is called best-effort before each
        # execute and must never raise into the loop.
        visualizer = bundle.make_action_visualizer(phone)

        def _flash(a) -> None:
            # Best-effort cursor/overlay flash before executing `a`; never raises into
            # the loop. No-op when the platform has no visualizer (iphone).
            if visualizer is not None and a is not None:
                try:
                    visualizer.show_action(a)
                except Exception:
                    pass

        # One-shot post-open hook (before the first observe): lets a caller prime
        # the just-connected session — e.g. the WebArena entry injects auth cookies,
        # starts HAR capture and navigates to the task start_url. Runs on the neutral
        # `phone` (device at phone.client); default None keeps iphone/chat untouched.
        # NOT wrapped in try: an opt-in caller wants a failed prime (bad cookies /
        # unreachable start_url) to surface, not run the task in a wrong state.
        if on_session_open is not None and callable(on_session_open):
            on_session_open(phone)

        # If the device exposes its exact window rect (browser via CDP), pin the HUD
        # into that window now that we are connected — the pre-connect CGWindowList
        # guess can pick the wrong same-named Chrome window. Same placement helper as
        # the factory (centered, ≈ iOS dock height), so the position is consistent.
        if hud is not None and hasattr(hud, "reposition"):
            _client = getattr(phone, "client", None)
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
            nonlocal stitch_acc
            if not _stop_requested():
                return None
            _drain_pending_read()
            _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                            context, seen_rows, turn_no=turn_no, say=_say)
            stitch_acc = None
            _say("\n收到 ESC：当前 turn 已收尾，agent-loop 安全停止")
            reason = "用户按 ESC 中止 agent-loop"
            if program is not None:
                return _finish(_orch_result(context, _interp, f"{reason}（任务未完成）", current=_cur_run))
            return _finish(_make_result(context, reason))

        def _drive_pending_reads(done_png: "bytes | None" = None) -> "str | None":
            """Pure single-frame READ runs — the inspect primitive. A `read` run is NOT a
            checker-gated milestone loop: it reads the result the prior milestone left, runs
            structured_read (读不到当没有，never blocks the program), packages the reads, and
            advances the interpreter — no actions, no acceptance gate.

            Critically, a read consumes the VERDICT FRAME — the exact frame the checker accepted
            the prior milestone on (passed in as `done_png`) — NOT a fresh capture a turn later.
            Transient result hints (a 检测-success toast, a green ✓ that fades) are visible on the
            verdict frame but can be gone by the next observe; re-capturing would read an empty
            screen and misjudge (user-reported flaw). At setup (no prior milestone) done_png is
            None → capture one frame here. Consecutive reads share the same screen, so the frame
            is reused across the loop. Returns the final reply if the program ended, else None."""
            nonlocal _cur_run, _run_idx, _notes_mark
            from gui_agent.core.orchestrator.engine import (
                package_result, task_type_for, to_milestone,
            )
            _frame = done_png  # verdict frame the checker just accepted on; reused across reads
            while _cur_run is not None and _cur_run.kind == "read":
                _reads: dict = {}
                if _cur_run.returns:
                    from gui_agent.core.orchestrator.structured_read import structured_read
                    if _frame is None:  # setup leading read: no prior frame → capture once
                        _frame = bundle.make_perception(
                            phone, log_dir / f"screenshot_read_{_run_idx}.png"
                        ).observe().png_bytes
                    _reads = structured_read(
                        _frame, _cur_run.returns,
                        read_spec=_cur_run.read_spec,
                        check_knowledge=getattr(supervisor, "_check_knowledge", "") or "",
                    )
                    _say(f"  [Orchestrator] 只读验收帧 {_cur_run.returns} → {_reads}")
                _res = package_result(
                    _cur_run, completed=True,
                    summary=f"读取 {'、'.join(_cur_run.returns) or _cur_run.name}",
                    notes=[], reads=_reads,
                )
                try:
                    _cur_run = _gen.send(_res)
                except StopIteration as _e:  # program finished after the read (finish / off end)
                    return _e.value or ""
                _run_idx += 1
            if _cur_run is not None:
                _ms = to_milestone(_cur_run, _run_idx)
                # fresh_advance only on a hand-off (a verdict frame was passed) — the leading
                # setup reseed (done_png=None, first milestone) must NOT skip its check.
                supervisor.reseed(
                    _ms, task_type=task_type_for(_cur_run),
                    fresh_advance=done_png is not None,
                )
                # Accumulate the executed milestone so the report's 子目标分解 sidebar names every
                # run (orchestrator reseeds one at a time; context.milestones is otherwise just the
                # first). Pure reads have no turns → not here; the 分解 program row shows them.
                if not any(m.get("id") == _ms.id for m in context.milestones):
                    context.milestones.append({
                        "id": _ms.id, "name": _ms.name, "description": _ms.description,
                        "kind": _ms.kind, "success_condition": _ms.success_condition,
                    })
                _notes_mark = len(context.content_notes)
            return None

        if program is not None:
            from gui_agent.core.orchestrator import Interpreter
            _interp = Interpreter(program)
            _orch_interp = _interp  # _save_ctx now mirrors its run_log (reads) into context
            # Persist the decomposed program so the report renders decompose as its OWN row
            # (a distinct stage now, not folded into turn 1's supervisor step).
            context.orchestrator = {
                "program": program.model_dump(mode="json"),
                "max_turns": max_turns,
            }
            _gen = _interp.steps()
            try:
                _cur_run = next(_gen)
            except StopIteration as _e:  # program with no run() (just finish / empty)
                return _finish(_orch_result(context, _interp, _e.value or ""))
            _reply = _drive_pending_reads()  # leading read(s) + reseed the first non-read run
            if _reply is not None:
                return _finish(_orch_result(context, _interp, _reply))

        while True:
            interrupted = _stop_after_esc(len(context.turns))
            if interrupted is not None:
                return interrupted

            turn_no = len(context.turns) + 1
            if turn_no > max_turns:
                _drain_pending_read()
                _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                                context, seen_rows, turn_no=turn_no - 1, say=_say)
                stitch_acc = None
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
            perception = bundle.make_perception(phone, log_dir / f"screenshot_turn_{turn_no}.png")
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
            # is set by the reseed inside _drive_pending_reads (fresh_advance). Behaviorally the
            # next milestone is decided on the exact frame the prior one was accepted on — same
            # as the verdict-frame carry-forward, just merged into this turn instead of the next.
            _orch_reply: "str | None" = None    # set if the program ended during a hand-off
            _did_loading = False
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
                _drain_pending_read()
                _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                                context, seen_rows, turn_no=turn_no, say=_say)
                stitch_acc = None
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
                # reads consume THIS verdict frame; reseed the next non-read milestone.
                _reply = _drive_pending_reads(done_png=observation.png_bytes)
                if _reply is not None:
                    _orch_reply = _reply
                    break
                _say(f"  [Orchestrator] 子目标「{_done_name}」完成 → 下一子任务："
                     f"{_cur_run.name}（同一验收帧上决策，不另起 turn）")
                # loop: re-decide the freshly-reseeded milestone on the same observation.

            if _orch_reply is not None:
                _drain_pending_read()
                # The program ended on this hand-off (last actionable milestone done → read /
                # finish). The merge only DROPS a verdict turn when it folds into a FOLLOWING
                # action; the terminal milestone has none, so record its verdict turn here —
                # otherwise the report is missing it and the verdict screenshot (already written
                # at observe) is orphaned. sv_step is the completed milestone's done verdict.
                context.turns.append(PolicyTurn(
                    index=turn_no,
                    observation_source=observation.source,
                    supervisor=sv_step,
                    action_decision=None,
                    checker=_extract_checker(supervisor),
                    planner=_extract_plan(supervisor),
                    replan=_extract_replan(supervisor),
                    executed=False,
                    llm_calls=get_llm_call_count() - llm_calls_before,
                    input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                    output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                    timings=getattr(supervisor, "_timings", {}),
                    token_usage=getattr(supervisor, "_token_usage", {}),
                    sections_loaded=list(getattr(supervisor, "_last_sections_loaded", []) or []),
                ))
                # Sync the last milestone's done verdict into context (no later turn body runs).
                sync_milestone_states(supervisor, context)
                return _finish(_orch_result(context, _interp, _orch_reply))

            # 页面未稳定（白屏/加载中）：等待并重新观察，本帧不写入 context.turns、不消耗
            # max_turns、不累加 noop_count。加载是 App 渲染延迟、不是 agent 的一步操作。
            # 连续加载设上限，防页面永挂导致死循环（旧行为：loading 帧既占轮数，又会因
            # should_act=False 累加 noop_count，连续 3 帧就误判"连续无动作"终止 agent）。
            if _did_loading:
                loading_streak += 1
                if loading_streak > MAX_LOADING_FRAMES:
                    _say(f"\n页面持续加载 {loading_streak} 帧仍未稳定，agent-loop 停止")
                    _term = f"页面持续加载未稳定（>{MAX_LOADING_FRAMES} 帧）"
                    if program is not None:
                        return _finish(_orch_result(context, _interp, _term, current=_cur_run))
                    return _finish(_make_result(context, _term))
                _say(f"  [Loading] 等待页面稳定（第 {loading_streak} 帧，不计入轮数）...")
                time.sleep(LOADING_WAIT_S)
                interrupted = _stop_after_esc(turn_no)
                if interrupted is not None:
                    return interrupted
                continue
            loading_streak = 0

            # Record which model each LLM config key actually used (once, self-describing
            # for cost — the report prefers this over re-resolving the active config later).
            if not context.models:
                from gui_agent.core.config import resolve_llm_config
                for _key in ("supervisor", "supervisor.decompose", "action_policy",
                             "reader", "output", "router", "back_nav"):
                    try:
                        context.models[_key] = resolve_llm_config(_key).model or ""
                    except Exception:
                        pass

            # Persist milestone decomposition after first step (DAG mode only — orchestrator mode
            # accumulates milestones per reseed in _drive_pending_reads, so don't overwrite).
            if program is None and not context.milestones and hasattr(supervisor, "_milestones"):
                context.milestones = [
                    {"id": m.id, "name": m.name, "description": m.description,
                     "kind": m.kind, "success_condition": m.success_condition}
                    for m in supervisor._milestones.values()
                ]

            if hasattr(supervisor, "task_type") and context.task_type is None:
                context.task_type = supervisor.task_type
                _say(f"任务类型: {context.task_type}")
            if sv_step.collection_scope and sv_step.collection_scope != context.collection_scope:
                context.collection_scope = sv_step.collection_scope
                _say(
                    "采集范围: "
                    + json.dumps(context.collection_scope.model_dump(exclude_none=True), ensure_ascii=False)
                )

            read_added_content = False
            read_note_hash = None

            # 先把上一轮后台 reader 读好的内容入库（此时已与上一轮动作/settle + 本轮截图/
            # loop_check 重叠完成，drain 几乎不等待 → 出块轮的 ~1.8s reader 被完全藏起）。
            _drain_pending_read()

            # 采集子目标切换 → 先 flush 上一个累积器的尾段（哪怕新子目标不采集，也别丢残留）。
            cur_mid = sv_step.milestone_id or "_global"
            if stitch_acc is not None and stitch_acc_mid != cur_mid:
                _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                                context, seen_rows, turn_no=turn_no, say=_say)
                stitch_acc = None
                stitch_acc_mid = None

            if sv_step.read_instruction and not sv_step.allow_read:
                _say(
                    "跳过读取入库: 当前阶段不允许采集 "
                    f"({sv_step.milestone_kind}/{sv_step.completion_strategy})"
                )
            elif sv_step.read_instruction:
                reader_instruction = build_reader_instruction(original_goal, sv_step)
                if sv_step.completion_strategy == "scroll_until_boundary":
                    # 多帧滚动采集：逐帧喂累积器拼接，几何去重；攒满约一屏才真正 reader 读，
                    # chunk 间留 overlap 防切行。read_added_content=拼接是否推进，供边界判定。
                    if stitch_acc is None:
                        stitch_acc = bundle.make_stitch_accumulator(overlap_px=STITCH_OVERLAP_PX)
                        stitch_acc_mid = cur_mid
                    stitch_acc_instr = reader_instruction
                    stitch_acc_sv = sv_step
                    chunks, advanced = stitch_acc.feed(observation.png_bytes)  # ~25ms，内联
                    read_added_content = advanced
                    if chunks:
                        # 后台读这些块（与动作/settle/下一轮 loop_check 重叠），下一轮 drain 入库。
                        _say(f"读取内容: {reader_instruction}（{len(chunks)} 拼接块后台读, "
                             f"待读 {stitch_acc.pending_px}px）")
                        pending_read = (
                            _READER_POOL.submit(
                                lambda cs=chunks, ins=reader_instruction:
                                    [reader.read(c, ins) for c in cs]
                            ),
                            turn_no, sv_step,
                        )
                    elif advanced:
                        _say(f"拼接累积中（{stitch_acc.pending_px}px，未满一屏，暂不读）")
                    else:
                        _say("列表未推进（滚动无效/到底），不追加")
                else:
                    # 单帧读取（read_once / 普通 analysis，无滚动重叠问题）→ 立即读。
                    _say(f"读取内容: {reader_instruction}")
                    note = reader.read(observation.png_bytes, reader_instruction)
                    if _store_chunk_note(note, context, seen_rows,
                                         turn_no=turn_no, sv_step=sv_step):
                        read_added_content = True
                        read_note_hash = _note_hash(context.content_notes[-1])
                        _say(f"内容摘要: {context.content_notes[-1][:80]}...")
                    else:
                        _say("内容摘要: 无新增/与已采集重复，未入库")

            action_decision = None
            executed = False
            probe_failed = False
            branch_settle_s: float | None = None  # 缓存滚动已在分支内 settle → 轮末跳过重复

            if sv_step.should_act:
                _say(f"动作指令: {sv_step.instruction}")
                if sv_step.preformed_action:
                    _say("使用预生成动作，跳过 Action Policy")
                    action_decision = sv_step.preformed_action
                else:
                    _status(turn_no, "动作决策中…")
                    _say("动作决策中...")
                    instruction_for_action = sv_step.instruction
                    profile_key = sv_step.milestone_id or "_global"
                    if (
                        sv_step.completion_strategy == "scroll_until_boundary"
                        and profile_key in scroll_probe_failures
                    ):
                        instruction_for_action = (
                            f"{instruction_for_action}\n\n"
                            "⚠️ 滚动探测反馈："
                            f"{scroll_probe_failures[profile_key]}。"
                            "请避免重复这些无效滚动落点/幅度，选择当前屏幕上更可能作用于主内容的滚动方式。"
                        )
                    _ap_t0 = time.perf_counter()
                    _ap_tok0 = get_llm_token_usage()
                    action_decision = action_policy.decide(
                        observation, instruction_for_action,
                        direction=sv_step.direction,
                        drag_column=sv_step.drag_column,
                        drag_steps=sv_step.drag_steps,
                    )
                    if hasattr(supervisor, "_timings"):
                        supervisor._timings["action_policy"] = time.perf_counter() - _ap_t0
                        supervisor._timings_order.append("action_policy")
                    if hasattr(supervisor, "_token_usage"):
                        _ap_in, _ap_out = get_llm_token_usage()
                        supervisor._token_usage["action_policy"] = {
                            "input": _ap_in - _ap_tok0[0], "output": _ap_out - _ap_tok0[1],
                        }
                    print_decision(
                        action_decision,
                        observation.png_bytes,
                        log_dir / f"structured_output_result_turn_{turn_no}.png",
                    )
                # Ensure YOLO/OCR prep finished before any execute/snap (covers
                # both the preformed-action and action-policy paths). Started
                # after screenshot, overlapped the decide → normally done already.
                prep_future.result()
                if action_decision.not_found_reason:
                    _say(f"  [NotFound] {action_decision.not_found_reason}")
                    _status(turn_no, "未找到目标元素")
                    executed = False
                else:
                    if action_decision.action:
                        _status(turn_no, f"[{action_label(action_decision.action.action_type)}] {action_decision.action.description}")
                    action = action_decision.action
                    profile_key = sv_step.milestone_id or "_global"
                    should_probe_scroll = (
                        action.action_type == "scroll"
                        and sv_step.completion_strategy == "scroll_until_boundary"
                    )
                    if should_probe_scroll and profile_key in scroll_profiles:
                        profile = scroll_profiles[profile_key]
                        cached = bundle.apply_scroll_profile(action, profile)
                        _say(
                            "  [ScrollProbe] 使用缓存滚动点: "
                            f"method={profile.method}, x={profile.x:.0f}, y={profile.y:.0f}, "
                            f"ticks={profile.ticks}, delta={profile.delta_px}"
                        )
                        _flash(cached)
                        if cached.action_type == "scroll":
                            print(f"\n动作: [{cached.action_type}] {cached.description}")
                            executor.execute_scroll(cached, ticks=profile.ticks, delta_px=profile.delta_px)
                        else:
                            executor.execute(ActionDecision(action=cached), app_name=sv_step.app_name or "")
                        # 验证缓存滚动是否真的动了：真机同一手势(尤其 MCP drag)会偶发不生效，
                        # turn1 滚了 turn2 没滚（20260530_155828）。不验证就会被下一轮 SimStuck
                        # 「冻结→边界」误判成到底、采集截断。**必须先 settle 再测**：滚动有延迟/
                        # 惯性，execute 后立刻截图屏幕还没动，会把每次缓存都误判成 0 位移而每轮空
                        # 重探（20260530_161048）。settle 等画面稳后再比，真滚→保留缓存、真没滚→重探。
                        branch_settle_s, _ = _settle_after_action(
                            phone, observation.png_bytes, cached.action_type
                        )
                        after_png = phone.screenshot()
                        cshift, _ = bundle.robust_shift(
                            bundle.gray_u8(observation.png_bytes), bundle.gray_u8(after_png)
                        )
                        if cshift != 0:
                            action = cached
                            action_decision = action_decision.model_copy(update={"action": action})
                            executed = True
                        else:
                            _say("  [ScrollProbe] 缓存滚动点 settle 后仍 0 位移 → 废弃缓存，重新探测")
                            scroll_profiles.pop(profile_key, None)
                            branch_settle_s = None  # 改走重探+轮末 settle
                    if should_probe_scroll and not executed and not probe_failed:
                        _flash(action)
                        probe = bundle.make_scroll_probe(phone, executor, log_dir)
                        result = probe.probe(observation.png_bytes, action, turn_no=turn_no)
                        if result.success and result.profile:
                            scroll_profiles[profile_key] = result.profile
                            scroll_probe_failures.pop(profile_key, None)
                            action = bundle.apply_scroll_profile(action, result.profile)
                            action_decision = action_decision.model_copy(update={"action": action})
                            executed = True
                        else:
                            probe_failed = True
                            scroll_probe_failures[profile_key] = result.reason
                            _say(
                                "  [ScrollProbe] 未找到可靠滚动点，停止本轮动作: "
                                f"{result.reason}"
                            )
                            executed = False
                    elif not should_probe_scroll:
                        # Flash where/what we're about to do (best-effort; cosmetic).
                        _flash(action)
                        executed = executor.execute(action_decision, app_name=sv_step.app_name or "", png_bytes=observation.png_bytes, is_home_screen=sv_step.is_home_screen)

            # Post-action targeting verify: did the snapped tap land on target?
            # Submit now so it runs concurrently with the settle below; resolved
            # there and stored on the turn for the next turn's off_target check.
            verify_future: Future | None = None
            verify_point = _snapped_point(action_decision) if executed else None
            if verify_point is not None and sv_step.instruction:
                verify_future = _VERIFY_POOL.submit(
                    verify_target, observation.png_bytes,
                    verify_point[0], verify_point[1], sv_step.instruction,
                )

            turn = PolicyTurn(
                index=turn_no,
                observation_source=observation.source,
                supervisor=sv_step,
                action_decision=action_decision,
                checker=_extract_checker(supervisor),
                planner=_extract_plan(supervisor),
                replan=_extract_replan(supervisor),
                executed=executed,
                llm_calls=get_llm_call_count() - llm_calls_before,
                input_tokens=get_llm_token_usage()[0] - tokens_before[0],
                output_tokens=get_llm_token_usage()[1] - tokens_before[1],
                read_added_content=read_added_content,
                read_note_hash=read_note_hash,
                timings=getattr(supervisor, "_timings", {}),
                token_usage=getattr(supervisor, "_token_usage", {}),
                sections_loaded=list(getattr(supervisor, "_last_sections_loaded", []) or []),
            )
            _print_timings(supervisor)
            context.turns.append(turn)
            sync_milestone_states(supervisor, context)
            _save_ctx()
            if not silent:
                _print_turn_stats(turn_no, turn_started_at, llm_calls_before)
            if on_turn and callable(on_turn):
                _entry: dict = {"no": turn.index, "summary": sv_step.summary, "executed": executed}
                if action_decision:
                    a = action_decision.action
                    _entry["action_type"] = a.action_type
                    _entry["action_desc"] = a.description
                    if action_decision.not_found_reason:
                        _entry["not_found"] = action_decision.not_found_reason
                on_turn(_entry)

            if sv_step.stop or sv_step.goal_completed:
                reason = sv_step.stop_reason or ("目标已达成" if sv_step.goal_completed else "agent-loop 停止")
                # 收尾：先 drain 本轮后台读，再读出累积器剩余不足一屏的内容，避免末尾几行丢失。
                _drain_pending_read()
                _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                                context, seen_rows, turn_no=turn_no, say=_say)
                stitch_acc = None
                if sv_step.goal_completed:
                    _say(f"\n目标已达成：{reason}")
                else:
                    _say(f"\n任务未完成：{reason}")
                # ── DSL orchestrator mode: a milestone's SUCCESS (goal_completed) hand-off is
                # merged into this turn by the decision-phase loop above, so reaching here means a
                # STOP — the milestone gave up / failed. Package it as a failed run; the
                # interpreter halts the program and we report. The DAG path (program is None) below
                # is unchanged.
                if program is not None:
                    from gui_agent.core.orchestrator.engine import package_result
                    _result = package_result(
                        _cur_run, completed=False,
                        summary=sv_step.summary or reason,
                        notes=context.content_notes[_notes_mark:],
                    )
                    try:
                        _cur_run = _gen.send(_result)
                    except StopIteration as _e:  # a failed run always halts the interpreter
                        return _finish(_orch_result(context, _interp, _e.value or ""))
                    # Defensive: a failed run halts the interpreter above; if the contract ever
                    # changes and it doesn't, summarize what we have rather than fall through.
                    return _finish(_orch_result(context, _interp, sv_step.summary or reason, current=_cur_run))
                if sv_step.goal_completed:
                    return _finish(_make_result(context, reason, sv_step.collection_summary))
                return _finish(_make_result(context, reason))

            if not (executed and auto_continue):
                interrupted = _stop_after_esc(turn_no)
                if interrupted is not None:
                    return interrupted

            if not executed and sv_step.should_act:
                if probe_failed:
                    noop_count += 1
                    if noop_count >= 3:
                        _say(f"\n连续 {noop_count} 轮滚动探测失败，agent-loop 停止")
                        return _finish(_make_result(context, f"连续 {noop_count} 轮滚动探测失败"))
                    _say("滚动探测失败，进入下一轮重新规划")
                    continue
                if action_decision and action_decision.not_found_reason:
                    noop_count += 1
                    if noop_count >= 3:
                        _say(f"\n连续 {noop_count} 轮无动作，agent-loop 停止")
                        return _finish(_make_result(context, f"连续 {noop_count} 轮无动作"))
                    continue
                return _finish(_make_result(context, "动作未执行，agent-loop 停止"))

            if sv_step.milestone_id != prev_milestone_id:
                noop_count = 0
            prev_milestone_id = sv_step.milestone_id

            if not sv_step.should_act:
                noop_count += 1
                if noop_count >= 3:
                    _say(f"\n连续 {noop_count} 轮无动作，agent-loop 停止")
                    return _finish(_make_result(context, f"连续 {noop_count} 轮无动作"))
                continue

            noop_count = 0

            if auto_continue:
                if branch_settle_s is not None:
                    # 缓存滚动已在分支内 settle 过（为验证位移），不重复等待。
                    turn.settle_s = branch_settle_s
                else:
                    _settle_act = action_decision.action if action_decision else None
                    settle_action_type = _settle_act.action_type if _settle_act else None
                    # type 是局部改动：把输入坐标的 y 传给 settle，只比输入行带、不被整帧稀释。
                    settle_focus_y = (
                        _settle_act.y
                        if (_settle_act and settle_action_type == "type" and _settle_act.y is not None)
                        else None
                    )
                    # tap 触发局部 UI 改动(菜单展开/下拉/勾选)：把点击点传给 settle，只看点击点附近
                    # box，避免局部变化被整帧稀释成「零效果」而误触发 replan。
                    settle_center = (
                        (_settle_act.x, _settle_act.y)
                        if (
                            _settle_act and settle_action_type == "tap"
                            and _settle_act.x is not None and _settle_act.y is not None
                        )
                        else None
                    )
                    turn.settle_s, turn.no_effect = _settle_after_action(
                        phone, observation.png_bytes, settle_action_type, settle_focus_y,
                        center=settle_center,
                    )
                if verify_future is not None:
                    try:
                        turn.target_verify = verify_future.result(timeout=8)
                        tv = turn.target_verify
                        if tv is not None and not tv.on_target:
                            _say(f"  [TargetVerify] off_target：标记落在「{tv.actual_element}」")
                    except Exception as e:
                        _say(f"  [TargetVerify] 校验失败（忽略）：{e}")
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
