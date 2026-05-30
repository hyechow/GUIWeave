"""CLI runner for policy experiments with two-layer architecture."""

import argparse
import hashlib
import json
import re
import sys
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator, Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from llm.structured import get_llm_call_count
from policy_expr.executor import ActionExecutor
from policy_expr.supervisor import MilestoneSupervisorPolicy, SimpleSupervisorPolicy
from policy_expr.supervisor.base import SupervisorPolicy
from policy_expr.output import generate_reply
from policy_expr.perception import LivePerception, LivePhoneSession
from policy_expr.reader import ContentReader, annotate_content_note, build_reader_instruction
from policy_expr.temporal import resolve_temporal_expressions
from policy_expr.policies import StructuredOutputPolicy
from policy_expr.policies.base import ActionPolicy
from policy_expr.scroll_probe import ScrollProfile, ScrollProbe, apply_profile
from policy_expr.stitch import StitchAccumulator
from policy_expr.target_verify import verify_target
from policy_expr.schemas import (
    ActionDecision,
    PolicyContext,
    PolicyTurn,
    action_label,
)
from policy_expr.visualize import print_decision
from policy_expr.hud import AgentHUD
from policy_expr.self_learning.app_summary import auto_discover_knowledge

POLICIES: dict[str, type[ActionPolicy]] = {
    StructuredOutputPolicy.name: StructuredOutputPolicy,
}

SUPERVISORS: dict[str, type[SupervisorPolicy]] = {
    SimpleSupervisorPolicy.name: SimpleSupervisorPolicy,
    MilestoneSupervisorPolicy.name: MilestoneSupervisorPolicy,
}

ROOT = Path(__file__).parent.parent
POLICY_LOG_ROOT = ROOT / "logs" / "policy_expr"
TURN_HEADER = "\033[1;36m--- Turn {turn_no} ---\033[0m"
TURN_STATS = "\033[2mTurn {turn_no} stats: llm_calls={llm_calls}, elapsed={elapsed:.2f}s\033[0m"
# 动作后自适应等待：轮询截图，等到屏幕「相对动作前帧变过、且相对上一帧停稳」再进入
# 下一轮决策。首帧间隔较长（SETTLE_FIRST_S），让 App 启动 zoom、页面横滑等转场动画
# 先跑完，避免在动画中途采样导致相邻两帧「碰巧相似」被误判停稳；之后用 SETTLE_UNIT_S
# 细粒度轮询。tap 冷启动会续等到效果出现，scroll 续等惯性停止，no-op 等到上限兜底。
SETTLE_FIRST_S = 1.0      # 首帧等待：覆盖大多数转场动画（zoom/横滑 ~0.3-0.5s）
SETTLE_UNIT_S = 0.5       # 后续轮询间隔
SETTLE_MAX_UNITS = 6
SETTLE_CHANGE_THR = 8.0   # 相对动作前帧的灰度均值差，超过即视为动作已生效（噪声地板 ~0.05）
SETTLE_STABLE_THR = 2.0   # 相对上一帧的灰度均值差，低于即视为画面已停稳
# drag/scroll 是页内操作，不触发页面加载/转场，但改动常局限一小块（如 picker 滚轮带），
# 全屏均值差测不出「changed」（实测 picker 拖动仅 0.1-0.25 << CHANGE_THR=8.0），用原
# 「变过且停稳」逻辑会每次顶满上限白等 ~4s。但也不能盲等固定时长：fling 惯性时长不定
# （轻拨 ~0.3s，重拨 1-2s），固定值太短会在滚轮没停稳时就截图，checker 读到滞后的标签
# 值（轮子已滑过、标签未更新），导致位移误算、来回震荡。故对 drag/scroll 只判「停稳」
# （相邻帧不再变化）、不判「changed」：轻拨很快返回，重拨等到真停，且保证读数准。
SETTLE_GESTURE_FIRST_S = 0.3  # drag/scroll 首帧：让惯性先跑起来，避免抬手瞬间误判停稳

# 动作重试机制暂时关闭：每轮只做一次 action policy 决策和执行。
# MAX_ACTION_RETRIES = 2        # 动作无效时最多重试次数
# ACTION_EFFECT_THRESHOLD = 3.0  # mean_image_diff 低于此值视为动作未生效


def _frame_diff(png_a: bytes, png_b: bytes) -> float:
    """两帧灰度图缩放到 160x320 后的平均绝对差（0-255 量级）。"""
    import io

    import numpy as np
    from PIL import Image

    a = np.array(Image.open(io.BytesIO(png_a)).convert("L").resize((160, 320)), dtype=np.float32)
    b = np.array(Image.open(io.BytesIO(png_b)).convert("L").resize((160, 320)), dtype=np.float32)
    return float(np.abs(a - b).mean())


def _settle_after_action(
    phone: LivePhoneSession, pre_frame: bytes | None, action_type: str | None = None
) -> float:
    """等到屏幕相对动作前帧「变过且停稳」，或达到上限。返回实际等待秒数。

    必须对照动作前帧：否则冷启动那 ~1s 静止旧画面会被误判为已就绪。

    drag/scroll 只判「停稳」（相邻帧不再变化）、不判「changed」：picker 改动小测不出
    changed 会顶满上限，而固定盲等又会在 fling 没停时截图导致读数滞后、来回震荡。
    """
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
                return dur
            if prev is not None and _frame_diff(prev, cur) < SETTLE_STABLE_THR:
                dur = time.perf_counter() - t0
                print(f"  [Settle] {dur:.1f}s ({i} 轮，停稳: {action_type})")
                return dur
            prev = cur
        dur = time.perf_counter() - t0
        print(f"  [Settle] {dur:.1f}s ({SETTLE_MAX_UNITS} 轮，达上限: {action_type})")
        return dur
    if pre_frame is None:
        time.sleep(SETTLE_FIRST_S)
        dur = time.perf_counter() - t0
        print(f"  [Settle] {dur:.1f}s (无动作前帧)")
        return dur
    prev: bytes | None = None
    for i in range(1, SETTLE_MAX_UNITS + 1):
        # 首帧等久一点让转场动画跑完，再用细粒度轮询。
        time.sleep(SETTLE_FIRST_S if i == 1 else SETTLE_UNIT_S)
        try:
            cur = phone.screenshot()
        except Exception:
            dur = time.perf_counter() - t0
            print(f"  [Settle] {dur:.1f}s ({i} 轮，截图异常提前返回)")
            return dur
        changed = _frame_diff(pre_frame, cur) > SETTLE_CHANGE_THR
        stable = prev is not None and _frame_diff(prev, cur) < SETTLE_STABLE_THR
        if changed and stable:
            dur = time.perf_counter() - t0
            print(f"  [Settle] {dur:.1f}s ({i} 轮，变过且停稳)")
            return dur
        prev = cur
    dur = time.perf_counter() - t0
    print(f"  [Settle] {dur:.1f}s ({SETTLE_MAX_UNITS} 轮，达上限)")
    return dur


# Post-action targeting verify runs in this 1-worker pool so it overlaps the
# settle wait (near-zero added latency). Daemon threads; finishes at process exit.
_VERIFY_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verify")

# YOLO detection + OCR run here, concurrent with the supervisor decide, so the
# snap has its boxes/text ready by execute time (~0.4s off the critical path).
_PREP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prep")


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


def create_run_dir(mode: str) -> Path:
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = POLICY_LOG_ROOT / mode / started_at
    suffix = 2
    while path.exists():
        path = POLICY_LOG_ROOT / mode / f"{started_at}_{suffix}"
        suffix += 1
    path.mkdir(parents=True, exist_ok=True)
    return path


class _TeeStream:
    """Write text to both the original stream and a log file."""

    def __init__(self, original: IO[str], log_file: IO[str]) -> None:
        self._original = original
        self._log_file = log_file
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "replace")

    def write(self, text: str) -> int:
        written = self._original.write(text)
        self._log_file.write(text)
        return written

    def flush(self) -> None:
        self._original.flush()
        self._log_file.flush()

    def isatty(self) -> bool:
        return self._original.isatty()

    def fileno(self) -> int:
        return self._original.fileno()

    def __getattr__(self, name: str) -> object:
        return getattr(self._original, name)


@contextmanager
def _tee_stdio(log_dir: Path) -> Iterator[None]:
    """Mirror stdout/stderr to per-run text logs while preserving terminal output."""

    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with (
        stdout_path.open("a", encoding="utf-8", buffering=1) as stdout_file,
        stderr_path.open("a", encoding="utf-8", buffering=1) as stderr_file,
        redirect_stdout(_TeeStream(sys.stdout, stdout_file)),
        redirect_stderr(_TeeStream(sys.stderr, stderr_file)),
    ):
        print(f"Stdout  : {stdout_path}")
        print(f"Stderr  : {stderr_path}")
        try:
            yield
        except Exception:
            traceback.print_exc()
            raise SystemExit(1) from None


def build_policy(name: str) -> ActionPolicy:
    try:
        return POLICIES[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(POLICIES))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc



def build_supervisor(name: str) -> SupervisorPolicy:
    try:
        return SUPERVISORS[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(SUPERVISORS))
        raise ValueError(f"未知监督者 {name!r}，可选：{choices}") from exc


def _save_context(path: Path, context: PolicyContext) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(context.model_dump_json(indent=2), encoding="utf-8")


def _extract_checker(supervisor: object) -> Optional[dict]:
    check = getattr(supervisor, "_last_check", None)
    if check is None:
        return None
    return check.model_dump(exclude_none=True)


def _extract_plan(supervisor: object) -> Optional[dict]:
    plan = getattr(supervisor, "_last_plan", None)
    if plan is None:
        return None
    return plan.model_dump(exclude_none=True)


def _extract_replan(supervisor: object) -> Optional[dict]:
    replan = getattr(supervisor, "_last_replan", None)
    if replan is None:
        return None
    return replan.model_dump(exclude_none=True)


def _load_context(
    path: Path,
    prompt: str,
    supervisor_name: str,
    action_name: str,
) -> PolicyContext:
    if path.exists():
        return PolicyContext.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return PolicyContext(
        goal=prompt,
        supervisor_policy_name=supervisor_name,
        action_policy_name=action_name,
    )


def _ensure_note_hashes(context: PolicyContext) -> None:
    if context.content_notes and not context.content_note_hashes:
        context.content_note_hashes = [_note_hash(note) for note in context.content_notes]


def _note_hash(note: str) -> str:
    normalized = re.sub(r"\s+", "", note.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ── 拼接采集：逐帧拼成长内容，满一屏才 reader 读，chunk 间留 overlap ──
STITCH_OVERLAP_PX = 150  # chunk 间重叠像素：防止行被切断；重叠区像素相同 → 行级去重可靠


def _store_chunk_note(
    note: str,
    context: PolicyContext,
    seen_rows: set[str],
    *,
    turn_no: int,
    sv_step,
) -> bool:
    """把一段拼接块的 reader 文本按行去重后入库。返回是否有新行入库。

    chunk 间 overlap 区像素完全相同 → reader 输出文本一致 → 精确行哈希去重可靠剔除重复行
    （区别于旧的逐帧重叠：那是不同像素、文本会抖、去不掉）。
    """
    if not note or note == "无相关内容":
        return False
    new_lines: list[str] = []
    for raw in note.splitlines():
        line = raw.strip()
        if not line:
            continue
        h = _note_hash(line)
        if h in seen_rows:
            continue
        seen_rows.add(h)
        new_lines.append(line)
    if not new_lines:
        return False
    stored = annotate_content_note(
        "\n".join(new_lines),
        turn_no=turn_no,
        sv_step=sv_step,
        collection_scope=context.collection_scope,
    )
    context.content_notes.append(stored)
    return True


def _flush_and_read(
    acc: "StitchAccumulator | None",
    instruction: str,
    sv_step,
    reader: "ContentReader",
    context: PolicyContext,
    seen_rows: set[str],
    *,
    turn_no: int,
    say,
) -> None:
    """采集子目标结束时吐出累积器尾段并读入库（剩余不足一屏的内容）。"""
    if acc is None or sv_step is None:
        return
    tail = acc.flush()
    if tail is None:
        return
    note = reader.read(tail, instruction or "")
    if _store_chunk_note(note, context, seen_rows, turn_no=turn_no, sv_step=sv_step):
        say(f"内容摘要(收尾块): {context.content_notes[-1][:80]}...")


def _make_result(
    context: PolicyContext,
    stop_reason: str,
    collection_context: str | None = None,
) -> dict:
    last_summary = context.turns[-1].supervisor.summary if context.turns else stop_reason
    turns_detail = []
    for t in context.turns:
        entry: dict = {"no": t.index, "summary": t.supervisor.summary, "executed": t.executed}
        if t.action_decision:
            a = t.action_decision.action
            entry["action_type"] = a.action_type
            entry["action_desc"] = a.description
            if t.action_decision.not_found_reason:
                entry["not_found"] = t.action_decision.not_found_reason
        turns_detail.append(entry)
    return {
        "goal": context.goal,
        "result_summary": last_summary,
        "stop_reason": stop_reason,
        "goal_completed": any(t.supervisor.goal_completed for t in context.turns),
        "turns_count": len(context.turns),
        "turns_detail": turns_detail,
        "content_notes": context.content_notes or None,
        "collection_context": collection_context,
        "collection_scope": context.collection_scope.model_dump(exclude_none=True)
        if context.collection_scope else None,
    }


def _print_turn_stats(turn_no: int, started_at: float, llm_calls_before: int) -> None:
    elapsed = time.perf_counter() - started_at
    llm_calls = get_llm_call_count() - llm_calls_before
    print(TURN_STATS.format(turn_no=turn_no, llm_calls=llm_calls, elapsed=elapsed))


def _print_timings(supervisor: SupervisorPolicy) -> None:
    timings = getattr(supervisor, "_timings", None)
    order = getattr(supervisor, "_timings_order", None)
    if not timings or not order:
        return
    parts = [f"{n}={timings[n]:.2f}s" for n in order]
    total = sum(timings.values())
    print(f"  [Timing] {' | '.join(parts)} | total={total:.2f}s")


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
) -> dict:
    def _say(s: str) -> None:
        if not silent:
            print(s)

    def _status(turn_no: int, msg: str) -> None:
        if hud:
            hud.update(f"Turn {turn_no} — {msg}")
        if live_state:
            live_state["current"] = msg

    context = _load_context(
        input_context_path or context_path,
        resolve_temporal_expressions(prompt),
        supervisor.name,
        action_policy.name,
    )
    _ensure_note_hashes(context)
    _save_context(context_path, context)
    _say(f"Goal    : {context.goal}")
    _say(f"Turns   : {len(context.turns)}")

    reader = ContentReader()
    original_goal = context.goal
    noop_count = 0
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

    with LivePhoneSession() as phone:
        executor = ActionExecutor(phone)

        while True:
            turn_no = len(context.turns) + 1
            if turn_no > max_turns:
                _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                                context, seen_rows, turn_no=turn_no - 1, say=_say)
                stitch_acc = None
                _say(f"\n达到最大轮数 {max_turns}，agent-loop 停止")
                _save_context(context_path, context)
                return _make_result(context, f"达到最大轮数 {max_turns}")

            turn_started_at = time.perf_counter()
            llm_calls_before = get_llm_call_count()

            _say("\n" + TURN_HEADER.format(turn_no=turn_no))

            _status(turn_no, "截图分析中…")
            perception = LivePerception(phone, log_dir / f"screenshot_turn_{turn_no}.png")
            observation = perception.observe()
            # YOLO + OCR run in the background, overlapping the decide below;
            # awaited just before execute (snap) so they add ~no latency.
            prep_future = _PREP_POOL.submit(executor.prepare_frame, observation.png_bytes)

            _status(turn_no, f"使用 {supervisor.name} supervisor 决策中…")
            _say("监督决策中...")
            sv_step = supervisor.step(observation, context.goal, context.turns)
            _say(f"监督者: {sv_step.summary}")
            _status(turn_no, sv_step.summary)

            # Persist milestone decomposition after first step
            if not context.milestones and hasattr(supervisor, "_milestones"):
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
                        stitch_acc = StitchAccumulator(overlap_px=STITCH_OVERLAP_PX)
                        stitch_acc_mid = cur_mid
                    stitch_acc_instr = reader_instruction
                    stitch_acc_sv = sv_step
                    chunks, advanced = stitch_acc.feed(observation.png_bytes)
                    read_added_content = advanced
                    if chunks:
                        _say(f"读取内容: {reader_instruction}（{len(chunks)} 拼接块, "
                             f"待读 {stitch_acc.pending_px}px）")
                        for chunk_png in chunks:
                            note = reader.read(chunk_png, reader_instruction)
                            if _store_chunk_note(note, context, seen_rows,
                                                 turn_no=turn_no, sv_step=sv_step):
                                read_note_hash = _note_hash(context.content_notes[-1])
                                _say(f"内容摘要(块): {context.content_notes[-1][:80]}...")
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
                    action_decision = action_policy.decide(
                        observation, instruction_for_action,
                        direction=sv_step.direction,
                        drag_column=sv_step.drag_column,
                    )
                    if hasattr(supervisor, "_timings"):
                        supervisor._timings["action_policy"] = time.perf_counter() - _ap_t0
                        supervisor._timings_order.append("action_policy")
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
                        action = apply_profile(action, profile)
                        action_decision = action_decision.model_copy(update={"action": action})
                        _say(
                            "  [ScrollProbe] 使用缓存滚动点: "
                            f"method={profile.method}, x={profile.x:.0f}, y={profile.y:.0f}, "
                            f"ticks={profile.ticks}, delta={profile.delta_px}"
                        )
                        if action.action_type == "scroll":
                            print(f"\n动作: [{action.action_type}] {action.description}")
                            executor.execute_scroll(action, ticks=profile.ticks, delta_px=profile.delta_px)
                        else:
                            executor.execute(ActionDecision(action=action), app_name=sv_step.app_name or "")
                        executed = True
                    elif should_probe_scroll:
                        probe = ScrollProbe(phone, executor, log_dir)
                        result = probe.probe(observation.png_bytes, action, turn_no=turn_no)
                        if result.success and result.profile:
                            scroll_profiles[profile_key] = result.profile
                            scroll_probe_failures.pop(profile_key, None)
                            action = apply_profile(action, result.profile)
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
                    else:
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
                read_added_content=read_added_content,
                read_note_hash=read_note_hash,
                timings=getattr(supervisor, "_timings", {}),
            )
            _print_timings(supervisor)
            context.turns.append(turn)
            _save_context(context_path, context)
            if not silent:
                _print_turn_stats(turn_no, turn_started_at, llm_calls_before)

            if sv_step.stop or sv_step.goal_completed:
                reason = sv_step.stop_reason or ("目标已达成" if sv_step.goal_completed else "agent-loop 停止")
                # 收尾：读出累积器里剩余不足一屏的内容，避免末尾几行丢失。
                _flush_and_read(stitch_acc, stitch_acc_instr, stitch_acc_sv, reader,
                                context, seen_rows, turn_no=turn_no, say=_say)
                stitch_acc = None
                if sv_step.goal_completed:
                    _say(f"\n目标已达成：{reason}")
                else:
                    _say(f"\n任务未完成：{reason}")
                _save_context(context_path, context)
                if sv_step.goal_completed:
                    return _make_result(context, reason, sv_step.collection_summary)
                return _make_result(context, reason)

            if not executed and sv_step.should_act:
                if probe_failed:
                    noop_count += 1
                    if noop_count >= 3:
                        _say(f"\n连续 {noop_count} 轮滚动探测失败，agent-loop 停止")
                        _save_context(context_path, context)
                        return _make_result(context, f"连续 {noop_count} 轮滚动探测失败")
                    _say("滚动探测失败，进入下一轮重新规划")
                    continue
                if action_decision and action_decision.not_found_reason:
                    noop_count += 1
                    if noop_count >= 3:
                        _say(f"\n连续 {noop_count} 轮无动作，agent-loop 停止")
                        _save_context(context_path, context)
                        return _make_result(context, f"连续 {noop_count} 轮无动作")
                    continue
                _save_context(context_path, context)
                return _make_result(context, "动作未执行，agent-loop 停止")

            if sv_step.milestone_id != prev_milestone_id:
                noop_count = 0
            prev_milestone_id = sv_step.milestone_id

            if not sv_step.should_act:
                noop_count += 1
                if noop_count >= 3:
                    _say(f"\n连续 {noop_count} 轮无动作，agent-loop 停止")
                    _save_context(context_path, context)
                    return _make_result(context, f"连续 {noop_count} 轮无动作")
                continue

            noop_count = 0

            if auto_continue:
                settle_action_type = (
                    action_decision.action.action_type
                    if action_decision and action_decision.action
                    else None
                )
                turn.settle_s = _settle_after_action(
                    phone, observation.png_bytes, settle_action_type
                )
                if verify_future is not None:
                    try:
                        turn.target_verify = verify_future.result(timeout=8)
                        tv = turn.target_verify
                        if tv is not None and not tv.on_target:
                            _say(f"  [TargetVerify] off_target：标记落在「{tv.actual_element}」")
                    except Exception as e:
                        _say(f"  [TargetVerify] 校验失败（忽略）：{e}")
                _save_context(context_path, context)  # 落盘 settle_s（+ target_verify）
                continue

            try:
                answer = input("继续下一轮？[Enter继续 / q退出] ").strip().lower()
            except EOFError:
                answer = ""
            if answer in {"q", "quit", "exit"}:
                _save_context(context_path, context)
                return _make_result(context, "用户退出 agent-loop")



def main() -> None:
    parser = argparse.ArgumentParser(description="测试手机策略运行模式")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="打开微信",
        help="目标指令，如「打开微信」「发一条朋友圈」",
    )
    parser.add_argument(
        "--policy",
        default=StructuredOutputPolicy.name,
        choices=sorted(POLICIES),
        help="动作策略模块",
    )
    parser.add_argument(
        "--supervisor",
        default=SimpleSupervisorPolicy.name,
        choices=sorted(SUPERVISORS),
        help="监督者策略模块",
    )
    parser.add_argument(
        "--context",
        type=Path,
        help="agent-loop 可选的 context 加载路径",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="agent-loop 最大自动执行轮数，防止无限循环",
    )
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="agent-loop 动作执行后自动进入下一轮；默认手动确认",
    )
    parser.add_argument(
        "--hud",
        action="store_true",
        help="在 iPhone 镜像窗口下方显示实时动作状态面板",
    )
    args = parser.parse_args()

    action_policy = build_policy(args.policy)
    supervisor = build_supervisor(args.supervisor)

    # Auto-discover app knowledge from goal
    knowledge = auto_discover_knowledge(args.prompt)
    if knowledge and hasattr(supervisor, "set_app_knowledge"):
        supervisor.set_app_knowledge(
            knowledge.navigation,
            app_name=knowledge.app_name,
            elements=knowledge.elements,
        )
        print(f"Knowledge: auto-loaded (nav={len(knowledge.navigation)} chars, elements={len(knowledge.elements)} chars), app={knowledge.app_name}")

    input_context_path = args.context
    log_dir = create_run_dir("agent-loop")
    context_path = log_dir / "context.json"
    hud = AgentHUD() if args.hud else None
    with _tee_stdio(log_dir):
        print(f"Log Dir : {log_dir}")
        print(f"Context : {input_context_path if input_context_path else None}")

        try:
            result: dict | None = run_agent_loop(
                args.prompt,
                action_policy,
                supervisor,
                input_context_path,
                log_dir,
                context_path,
                max_turns=args.max_turns,
                auto_continue=args.auto_continue,
                hud=hud,
            )
            if result:
                output = generate_reply(
                    result["goal"],
                    result,
                    content_notes=result.get("content_notes"),
                    collection_context=result.get("collection_context"),
                )
                print("\n" + "=" * 50)
                print("最终输出")
                print("=" * 50)
                print(output.rstrip())
                print("=" * 50)

            # Auto-generate HTML report
            if (log_dir / "context.json").exists():
                try:
                    from scripts.report_builder import RunnerReportBuilder, save_report
                    report_data = RunnerReportBuilder().build(log_dir)
                    report_path = save_report(report_data, log_dir / "report.html")
                    print(f"\nReport  : {report_path}")
                except Exception as exc:
                    print(f"\nReport  : 生成失败 ({exc})")
        finally:
            if hud:
                hud.close()


if __name__ == "__main__":
    main()
