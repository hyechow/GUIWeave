"""Multi-turn conversational CLI for iPhone automation agent."""

from __future__ import annotations

import argparse
import re
import sys
import time
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Iterator

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import ANSI

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from rich.box import ROUNDED
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.executor import ActionExecutor
from policy_expr.output import render_final_output
from policy_expr.perception import LivePerception, LivePhoneSession
from policy_expr.reader import ContentReader, annotate_content_note, build_reader_instruction
from policy_expr.schemas import (
    Observation,
    PolicyContext,
    PolicyTurn,
)
from policy_expr.supervisor import SimpleSupervisorPolicy
from policy_expr.supervisor.base import SupervisorPolicy
from policy_expr.visualize import print_decision

from policy_expr.policies import StructuredOutputPolicy
from policy_expr.policies.base import ActionPolicy
from policy_expr.runner import (
    POLICY_LOG_ROOT,
    _TeeStream,
    build_policy,
    build_supervisor,
    create_run_dir,
)

ROOT = Path(__file__).parent.parent

console = Console()


# ── Router ──────────────────────────────────────────────────────────────────


class RouterResult(BaseModel):
    actionable: bool = Field(description="该指令是否需要操控手机才能完成")
    reason: str = Field(default="", description="判断理由（actionable=true 时留空）")
    reply: str = Field(default="", description="actionable=false 时的回复；闲聊等无关问题留空")


ROUTER_SYSTEM = """\
你是 iPhone 自动化助手的意图分类器。判断用户的指令是否需要通过操控手机来完成。

分类标准：
- 需要操控手机：打开 app、点击按钮、发送消息、拍照、设置闹钟等 → actionable=true
- 不需要操控手机但可根据对话历史回答（如"我刚才做了什么""之前成功了吗"）→ actionable=false，reply 中给出回答
- 关于自身的问题（"你是谁""你叫什么""你能做什么"等）→ actionable=false，用自然语言自我介绍（你叫 Lucas，是一个 iPhone GUI Agent，可以帮用户操控 iPhone 完成各种任务），reply 不要照抄本段说明
- 无关问题（闲聊、知识问答等）→ actionable=false，reply 留空
- 边界模糊时倾向放行（actionable=true），让执行 agent 去判断

请用中文回答。
"""


def route_message(user_msg: str, session_history: list[dict]) -> RouterResult:
    cfg = resolve_llm_config("router")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)

    history_text = _format_session_history(session_history) if session_history else "（无历史）"
    messages = [
        SystemMessage(content=ROUTER_SYSTEM),
        HumanMessage(content=f"对话历史：\n{history_text}\n\n当前用户指令：{user_msg}"),
    ]
    return invoke_structured(llm, messages, RouterResult)


# ── History injection ──────────────────────────────────────────────────────


def _format_session_history(history: list[dict]) -> str:
    if not history:
        return "（无历史）"
    lines = []
    for i, entry in enumerate(history, 1):
        status = "✓" if entry.get("goal_completed") else "✗"
        lines.append(
            f"{i}. 用户说「{entry['user_msg']}」→ {status} {entry['result_summary']}"
        )
    return "\n".join(lines)


def _build_goal_with_history(user_msg: str, session_history: list[dict]) -> str:
    if not session_history:
        return user_msg
    history_text = _format_session_history(session_history)
    return f"之前的对话历史：\n{history_text}\n\n当前用户指令：{user_msg}"


# ── Agent loop for chat ───────────────────────────────────────────────────


class _SilentTeeStream(_TeeStream):
    """Like _TeeStream but suppresses output to the original terminal stream."""

    def write(self, text: str) -> int:
        return self._log_file.write(text)

    def flush(self) -> None:
        self._log_file.flush()

    def fileno(self) -> int:
        return self._log_file.fileno()


@contextmanager
def _silent_stdio(log_dir: Path) -> Iterator[None]:
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with (
        stdout_path.open("a", encoding="utf-8", buffering=1) as stdout_file,
        stderr_path.open("a", encoding="utf-8", buffering=1) as stderr_file,
        redirect_stdout(_SilentTeeStream(sys.stdout, stdout_file)),
        redirect_stderr(_SilentTeeStream(sys.stderr, stderr_file)),
    ):
        try:
            yield
        except Exception:
            traceback.print_exc()
            raise


def _save_context(path: Path, context: PolicyContext) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(context.model_dump_json(indent=2), encoding="utf-8")


def _ensure_note_hashes(context: PolicyContext) -> None:
    if context.content_notes and not context.content_note_hashes:
        context.content_note_hashes = [
            re.sub(r"\s+", "", note.strip().lower())[:64] for note in context.content_notes
        ]


def _note_hash(note: str) -> str:
    import hashlib
    normalized = re.sub(r"\s+", "", note.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def run_chat_turn(
    goal: str,
    action_policy: ActionPolicy,
    supervisor: SupervisorPolicy,
    log_dir: Path,
    max_turns: int = 20,
    live_state: dict | None = None,
) -> dict:
    context = PolicyContext(
        goal=goal,
        supervisor_policy_name=supervisor.name,
        action_policy_name=action_policy.name,
    )
    context_path = log_dir / "context.json"
    _ensure_note_hashes(context)
    _save_context(context_path, context)

    reader = ContentReader()
    noop_count = 0

    from policy_expr.hud import AgentHUD

    with _silent_stdio(log_dir), LivePhoneSession() as phone, AgentHUD() as hud:
        executor = ActionExecutor(phone)

        while True:
            turn_no = len(context.turns) + 1
            if turn_no > max_turns:
                return _make_result(context, f"达到最大轮数 {max_turns}")

            perception = LivePerception(phone, log_dir / f"screenshot_turn_{turn_no}.png")
            observation = perception.observe()

            hud.update(f"Turn {turn_no} | 思考中…")
            if live_state:
                live_state["current"] = "观察屏幕…"

            sv_step = supervisor.step(observation, context.goal, context.turns)
            hud.update(f"Turn {turn_no} | {sv_step.summary}")

            if live_state:
                live_state["current"] = sv_step.summary

            if hasattr(supervisor, "task_type") and context.task_type is None:
                context.task_type = supervisor.task_type

            read_note_hash = None

            if sv_step.read_instruction and sv_step.allow_read:
                reader_instruction = build_reader_instruction(goal, sv_step)
                note = reader.read(observation.png_bytes, reader_instruction)
                if note and note != "无相关内容":
                    note = annotate_content_note(
                        note, turn_no=turn_no, sv_step=sv_step,
                        collection_scope=context.collection_scope,
                    )
                    read_note_hash = _note_hash(note)
                    if read_note_hash not in context.content_note_hashes:
                        context.content_note_hashes.append(read_note_hash)
                        context.content_notes.append(note)

            action_decision = None
            executed = False

            if sv_step.should_act:
                if live_state:
                    live_state["current"] = sv_step.instruction or "执行动作…"

                if sv_step.preformed_action:
                    action_decision = sv_step.preformed_action
                else:
                    action_decision = action_policy.decide(observation, sv_step.instruction)
                    print_decision(
                        action_decision, observation.png_bytes,
                        log_dir / f"structured_output_result_turn_{turn_no}.png",
                    )
                if action_decision.not_found_reason:
                    executed = False
                else:
                    executed = executor.execute(action_decision, app_name=sv_step.app_name or "")

            if live_state and executed:
                live_state["current"] = "等待下一轮…"

            turn = PolicyTurn(
                index=turn_no,
                observation_source=observation.source,
                supervisor=sv_step,
                action_decision=action_decision,
                executed=executed,
            )
            context.turns.append(turn)
            _save_context(context_path, context)

            if sv_step.stop or sv_step.goal_completed:
                reason = sv_step.stop_reason or (
                    "目标已达成" if sv_step.goal_completed else "停止"
                )
                render_final_output(
                    goal, supervisor.name, context.turns, log_dir, reason,
                    content_notes=context.content_notes or None,
                    collection_context=sv_step.collection_summary,
                )
                return _make_result(context, reason)

            if not executed and sv_step.should_act:
                if action_decision and action_decision.not_found_reason:
                    noop_count += 1
                    if noop_count >= 3:
                        return _make_result(context, f"连续 {noop_count} 轮无动作")
                    continue
                return _make_result(context, "动作未执行")

            if not sv_step.should_act:
                noop_count += 1
                if noop_count >= 3:
                    return _make_result(context, f"连续 {noop_count} 轮无动作")
                continue

            noop_count = 0
            time.sleep(1.5)


_ACTION_STYLE = {
    "tap": "bright_blue",
    "type": "bright_green",
    "scroll": "bright_magenta",
    "home": "bright_yellow",
}


def _make_result(context: PolicyContext, stop_reason: str) -> dict:
    last_summary = ""
    if context.turns:
        last_summary = context.turns[-1].supervisor.summary

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
        "result_summary": last_summary or stop_reason,
        "stop_reason": stop_reason,
        "goal_completed": any(t.supervisor.goal_completed for t in context.turns),
        "turns_count": len(context.turns),
        "turns_detail": turns_detail,
    }


# ── UI ─────────────────────────────────────────────────────────────────────


_HEADER = """\
[cyan] _
| |   _   _  ___ __ _ ___
| |  | | | |/ __/ _` / __|
| |__| |_| | (__| (_| \\__ \\
|____/\\__,_|\\___\\__,_|___/[/]  [dim]iPhone GUI Agent[/dim]
"""

def _print_header() -> None:
    console.print()
    console.print(_HEADER)
    console.print("[dim]  /exit  ·  /clear[/dim]")
    console.print()


_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _SpinnerLine:
    """Live-compatible renderable: animates a braille spinner with current status text."""

    def __init__(self, state: dict) -> None:
        self._state = state
        self._t0 = time.time()

    def __rich_console__(self, console, options):  # noqa: ARG002
        frame = _SPINNER_FRAMES[int((time.time() - self._t0) * 10) % len(_SPINNER_FRAMES)]
        current = self._state.get("current", "…")
        yield Text(f"  {frame}  {current}", style="cyan")


def _print_reply(text: str) -> None:
    console.print()
    console.print(
        Panel(
            Text(text, style="white"),
            border_style="bright_black",
            box=ROUNDED,
            padding=(0, 2),
        )
    )
    console.print()


def _print_result(result: dict) -> None:
    ok = result["goal_completed"]
    color = "green" if ok else "red"
    border = "green" if ok else "red"
    icon = "✓" if ok else "✗"
    label = "done" if ok else "failed"

    tree = Tree(f"[bold {color}]{icon}  {result['result_summary']}[/bold {color}]")

    for t in result.get("turns_detail", []):
        atype = t.get("action_type")
        if t.get("not_found"):
            tree.add(
                f"[dim]Turn {t['no']}[/dim]  [yellow]{t['not_found']}[/yellow]"
            )
        elif atype and t["executed"]:
            style = _ACTION_STYLE.get(atype, "white")
            tree.add(
                f"[dim]Turn {t['no']}[/dim]  [{style}]{atype}[/] {t['action_desc']}"
            )
        elif atype and not t["executed"]:
            tree.add(f"[dim]Turn {t['no']}[/dim]  [dim]{atype} (未执行)[/dim]")
        else:
            tree.add(f"[dim]Turn {t['no']}[/dim]  {t['summary']}")

    turns = result["turns_count"]
    if turns:
        tree.add(f"[dim]{turns} turns[/dim]")

    console.print()
    console.print(
        Panel(
            tree,
            border_style=border,
            box=ROUNDED,
            title=f"[dim bold]{label}[/dim bold]",
            padding=(0, 1),
        )
    )
    console.print()


# ── Main loop ──────────────────────────────────────────────────────────────


_COMMANDS = ["/exit", "/clear"]
_completer = WordCompleter(_COMMANDS, meta_dict={"/exit": "退出", "/clear": "清空历史"})
_pt_prompt = ANSI("\033[1;36m❯ \033[0m")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lucas 多轮对话")
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()

    action_policy = build_policy(StructuredOutputPolicy.name)
    supervisor = build_supervisor(SimpleSupervisorPolicy.name)

    _print_header()
    session: list[dict] = []

    while True:
        try:
            user_msg = pt_prompt(_pt_prompt, completer=_completer).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not user_msg:
            continue

        if user_msg == "/exit":
            console.print("[dim]再见[/dim]")
            break

        if user_msg == "/clear":
            console.clear()
            session.clear()
            _print_header()
            continue

        # Route
        try:
            router_result = route_message(user_msg, session)
        except Exception:
            router_result = RouterResult(actionable=True, reason="分类失败，放行")

        if not router_result.actionable:
            _print_reply(router_result.reply or "我只能帮你操控 iPhone，请告诉我需要做什么。")
            session.append({
                "user_msg": user_msg,
                "result_summary": router_result.reply or router_result.reason,
                "stop_reason": "非手机操作",
                "goal_completed": False,
                "turns_count": 0,
            })
            continue

        # Execute
        goal = _build_goal_with_history(user_msg, session)
        log_dir = create_run_dir("chat")

        live_state: dict = {"current": "连接中..."}

        with Live(
            _SpinnerLine(live_state),
            console=console,
            refresh_per_second=10,
            transient=False,
        ):
            try:
                result = run_chat_turn(
                    goal, action_policy, supervisor, log_dir,
                    max_turns=args.max_turns, live_state=live_state,
                )
            except SystemExit:
                raise
            except Exception as exc:
                result = {
                    "result_summary": str(exc),
                    "stop_reason": f"异常: {exc}",
                    "goal_completed": False,
                    "turns_count": 0,
                }

        _print_result(result)

        session.append({
            "user_msg": user_msg,
            "result_summary": result["result_summary"],
            "stop_reason": result["stop_reason"],
            "goal_completed": result["goal_completed"],
            "turns_count": result["turns_count"],
            "log_dir": str(log_dir),
        })


if __name__ == "__main__":
    main()
