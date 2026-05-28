"""CLI runner for policy experiments with two-layer architecture."""

import argparse
import hashlib
import json
import re
import sys
import time
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator

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
from policy_expr.recon.yolo_calibrator import YoloCalibrator
from policy_expr.reader import ContentReader, annotate_content_note, build_reader_instruction
from policy_expr.policies import StructuredOutputPolicy
from policy_expr.policies.base import ActionPolicy
from policy_expr.schemas import (
    Observation,
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

# 动作重试机制暂时关闭：每轮只做一次 action policy 决策和执行。
# MAX_ACTION_RETRIES = 2        # 动作无效时最多重试次数
# ACTION_EFFECT_THRESHOLD = 3.0  # mean_image_diff 低于此值视为动作未生效


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


def run_once(
    prompt: str,
    action_policy: ActionPolicy,
    supervisor: SupervisorPolicy,
    log_dir: Path,
    context_path: Path,
    hud: AgentHUD | None = None,
) -> dict:
    context = PolicyContext(
        goal=prompt,
        supervisor_policy_name=supervisor.name,
        action_policy_name=action_policy.name,
    )
    _save_context(context_path, context)
    print(f"Goal    : {context.goal}")
    print(f"Turns   : {len(context.turns)}")

    with LivePhoneSession() as phone:
        turn_started_at = time.perf_counter()
        llm_calls_before = get_llm_call_count()

        if hud: hud.update("Turn 1 — 截图分析中…")
        perception = LivePerception(phone, log_dir / "screenshot.png")
        observation = perception.observe()
        calibrator = YoloCalibrator.from_png(observation.png_bytes)

        if hud: hud.update(f"Turn 1 — 使用 {supervisor.name} supervisor 决策中…")
        print("监督决策中...")
        sv_step = supervisor.step(observation, context.goal, context.turns)
        print(f"监督者: {sv_step.summary}")
        if hud: hud.update(f"Turn 1 — {sv_step.summary}")

        # Persist milestone decomposition
        if not context.milestones and hasattr(supervisor, "_milestones"):
            context.milestones = [
                {"id": m.id, "name": m.name, "description": m.description,
                 "kind": m.kind, "success_condition": m.success_condition}
                for m in supervisor._milestones.values()
            ]

        action_decision = None
        executed = False

        if sv_step.should_act:
            print(f"动作指令: {sv_step.instruction}")
            if hud: hud.update("Turn 1 — 动作决策中…")
            print("动作决策中...")
            _ap_t0 = time.perf_counter()
            action_decision = action_policy.decide(
                observation, sv_step.instruction,
                direction=sv_step.direction,
                drag_column=sv_step.drag_column,
            )
            if hasattr(supervisor, "_timings"):
                supervisor._timings["action_policy"] = time.perf_counter() - _ap_t0
                supervisor._timings_order.append("action_policy")
            print_decision(action_decision, observation.png_bytes, log_dir / "structured_output_result.png")
            if hud:
                a = action_decision.action
                hud.update(f"Turn 1 — [{action_label(a.action_type)}] {a.description}")
            executed = ActionExecutor(phone, calibrator).execute(action_decision, app_name=sv_step.app_name or "")

        turn = PolicyTurn(
            index=1,
            observation_source=observation.source,
            supervisor=sv_step,
            action_decision=action_decision,
            executed=executed,
            timings=getattr(supervisor, "_timings", {}),
        )
        _print_timings(supervisor)
        context.turns.append(turn)
        _save_context(context_path, context)

        if executed:
            time.sleep(1.5)
            after_bytes = phone.screenshot()
            after_obs = Observation(png_bytes=after_bytes, source="live")
            after_path = log_dir / "screenshot_after.png"
            after_path.write_bytes(after_bytes)
            print(f"已保存: {after_path}")

            print("验证中...")
            confirm = supervisor.step(after_obs, context.goal, context.turns)
            context.turns.append(PolicyTurn(
                index=2,
                observation_source="live",
                supervisor=confirm,
                action_decision=None,
                executed=False,
            ))
            stop_reason = confirm.stop_reason or "single-step 完成一轮后停止"
        else:
            stop_reason = "动作未执行，single-step 停止"

        _save_context(context_path, context)
        _print_turn_stats(1, turn_started_at, llm_calls_before)
        return _make_result(context, stop_reason)


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
        prompt,
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

    with LivePhoneSession() as phone:
        executor = ActionExecutor(phone)

        while True:
            turn_no = len(context.turns) + 1
            if turn_no > max_turns:
                _say(f"\n达到最大轮数 {max_turns}，agent-loop 停止")
                _save_context(context_path, context)
                return _make_result(context, f"达到最大轮数 {max_turns}")

            turn_started_at = time.perf_counter()
            llm_calls_before = get_llm_call_count()

            _say("\n" + TURN_HEADER.format(turn_no=turn_no))

            _status(turn_no, "截图分析中…")
            perception = LivePerception(phone, log_dir / f"screenshot_turn_{turn_no}.png")
            observation = perception.observe()
            executor.calibrator = YoloCalibrator.from_png(observation.png_bytes)

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

            if sv_step.read_instruction and not sv_step.allow_read:
                _say(
                    "跳过读取入库: 当前阶段不允许采集 "
                    f"({sv_step.milestone_kind}/{sv_step.completion_strategy})"
                )
            elif sv_step.read_instruction:
                reader_instruction = build_reader_instruction(original_goal, sv_step)
                _say(f"读取内容: {reader_instruction}")
                note = reader.read(observation.png_bytes, reader_instruction)
                if note and note != "无相关内容":
                    note = annotate_content_note(
                        note,
                        turn_no=turn_no,
                        sv_step=sv_step,
                        collection_scope=context.collection_scope,
                    )
                    read_note_hash = _note_hash(note)
                    if read_note_hash not in context.content_note_hashes:
                        context.content_note_hashes.append(read_note_hash)
                        context.content_notes.append(note)
                        read_added_content = True
                        _say(f"内容摘要: {note[:80]}...")
                    else:
                        _say("内容摘要: 与已采集内容重复，未入库")

            action_decision = None
            executed = False

            if sv_step.should_act:
                _say(f"动作指令: {sv_step.instruction}")
                if sv_step.preformed_action:
                    _say("使用预生成动作，跳过 Action Policy")
                    action_decision = sv_step.preformed_action
                else:
                    _status(turn_no, "动作决策中…")
                    _say("动作决策中...")
                    _ap_t0 = time.perf_counter()
                    action_decision = action_policy.decide(
                        observation, sv_step.instruction,
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
                if action_decision.not_found_reason:
                    _say(f"  [NotFound] {action_decision.not_found_reason}")
                    _status(turn_no, "未找到目标元素")
                    executed = False
                else:
                    if action_decision.action:
                        _status(turn_no, f"[{action_label(action_decision.action.action_type)}] {action_decision.action.description}")
                    executed = executor.execute(action_decision, app_name=sv_step.app_name or "")

            turn = PolicyTurn(
                index=turn_no,
                observation_source=observation.source,
                supervisor=sv_step,
                action_decision=action_decision,
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
                if sv_step.goal_completed:
                    _say(f"\n目标已达成：{reason}")
                else:
                    _say(f"\n任务未完成：{reason}")
                _save_context(context_path, context)
                if sv_step.goal_completed:
                    return _make_result(context, reason, sv_step.collection_summary)
                return _make_result(context, reason)

            if not executed and sv_step.should_act:
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
                time.sleep(1.5)
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
        "--mode",
        default="single-step",
        choices=["single-step", "agent-loop"],
        help="运行模式：single-step 单步；agent-loop 多步自动循环",
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

    mode = args.mode
    input_context_path = args.context
    log_dir = create_run_dir(mode)
    context_path = log_dir / "context.json"
    hud = AgentHUD() if args.hud else None
    with _tee_stdio(log_dir):
        print(f"Log Dir : {log_dir}")
        print(f"Context : {input_context_path if input_context_path else None}")

        try:
            result: dict | None = None
            if mode == "single-step":
                if input_context_path is not None:
                    raise ValueError("--context 目前只支持 agent-loop 模式")
                result = run_once(args.prompt, action_policy, supervisor, log_dir, context_path, hud=hud)
            elif mode == "agent-loop":
                result = run_agent_loop(
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
