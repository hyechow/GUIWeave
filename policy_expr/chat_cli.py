"""Multi-turn conversational CLI for iPhone automation agent."""

from __future__ import annotations

import argparse
import os
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

from rich.box import ROUNDED
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from policy_expr.self_learning.app_summary import auto_discover_knowledge
from policy_expr.supervisor import MilestoneSupervisorPolicy, SimpleSupervisorPolicy
from policy_expr.policies import StructuredOutputPolicy
from policy_expr.runner import _TeeStream, build_policy, build_supervisor, run_agent_loop
from policy_expr.chat_session import (
    RouterResult,
    generate_reply,
    route_message,
)
from policy_expr.prefs import PreferenceManager
from policy_expr.session_recorder import SessionRecorder

ROOT = Path(__file__).parent.parent

console = Console()



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


def run_chat_turn(
    goal: str,
    action_policy,
    supervisor,
    log_dir: Path,
    max_turns: int = 20,
    live_state: dict | None = None,
    backend: str = "daemon",
    on_turn: object = None,
) -> dict:
    """Thin wrapper around run_agent_loop with silent stdio, HUD and live_state spinner."""
    from policy_expr.hud import AgentHUD

    context_path = log_dir / "context.json"
    with _silent_stdio(log_dir), AgentHUD() as hud:
        return run_agent_loop(
            goal, action_policy, supervisor,
            input_context_path=None,
            log_dir=log_dir,
            context_path=context_path,
            max_turns=max_turns,
            auto_continue=True,
            hud=hud,
            live_state=live_state,
            silent=True,
            backend=backend,
            on_turn=on_turn,
        )


_ACTION_STYLE = {
    "tap": "bright_blue",
    "type": "bright_green",
    "scroll": "bright_magenta",
    "home": "bright_yellow",
}


# ── UI ─────────────────────────────────────────────────────────────────────


_HEADER_ART = [
    ("bold bright_cyan", "  ██╗     ██╗   ██╗  ██████╗  █████╗  ███████╗"),
    ("bold bright_cyan", "  ██║     ██║   ██║ ██╔════╝ ██╔══██╗ ██╔════╝"),
    ("bold cyan",        "  ██║     ██║   ██║ ██║      ███████║ ███████╗ "),
    ("bold cyan",        "  ██║     ██║   ██║ ██║      ██╔══██║ ╚════██║ "),
    ("bold blue",        "  ███████╗╚██████╔╝ ╚██████╗ ██║  ██║ ███████║ "),
    ("bold blue",        "  ╚══════╝ ╚═════╝   ╚═════╝ ╚═╝  ╚═╝ ╚══════╝"),
]

def _print_header() -> None:
    console.print()
    for style, line in _HEADER_ART:
        console.print(f"[{style}]{line}[/]")
    console.print()
    console.print("  [bold bright_cyan]iPhone GUI Agent[/]  [dim]─  自动操控 iPhone 的智能助手[/]")
    console.print("  [dim]操作各类 App · 发消息 · 点外卖 · 搜索内容 · 更多…[/]")
    console.print()
    console.print("  [dim]/exit  退出  ·  /clear  清空历史  ·  /supervisor  切换策略引擎[/]")
    console.print("  [dim]/mode [silent|standard]  切换操作模式  ·  /model [qwen35|qwen36]  切换模型[/]")
    console.print()


_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _SpinnerLine:
    """Live-compatible renderable: animates a braille spinner with current status text."""

    def __init__(self, state: dict) -> None:
        self._state = state
        self._t0 = time.time()

    def __rich_console__(self, console, options):  # noqa: ARG002
        current = self._state.get("current", "…")
        if self._state.get("done"):
            yield Text(f"  ✓  {current}", style="dim")
        else:
            frame = _SPINNER_FRAMES[int((time.time() - self._t0) * 10) % len(_SPINNER_FRAMES)]
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


def _turn_line(t: dict) -> str:
    """Format a single turn entry as a Rich markup string."""
    atype = t.get("action_type")
    no = t.get("no", "?")
    if t.get("not_found"):
        return f"  [dim]Turn {no}[/dim]  [yellow]{t['not_found']}[/yellow]"
    if atype and t.get("executed"):
        style = _ACTION_STYLE.get(atype, "white")
        return f"  [dim]Turn {no}[/dim]  [{style}]{atype}[/]  [dim]{t.get('action_desc', '')}[/dim]"
    if atype:
        return f"  [dim]Turn {no}[/dim]  [dim]{atype} (未执行)[/dim]"
    return f"  [dim]Turn {no}[/dim]  [dim]{t.get('summary', '')}[/dim]"


def _print_result(result: dict) -> None:
    ok = result["goal_completed"]
    color = "green" if ok else "red"
    border = "green" if ok else "red"
    icon = "✓" if ok else "✗"
    label = "done" if ok else "failed"

    turns = result.get("turns_count", 0)
    suffix = f"  [dim]{turns} turns[/dim]" if turns else ""
    tree = Tree(f"[bold {color}]{icon}  {result['result_summary']}[/bold {color}]{suffix}")

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


_COMMANDS = ["/exit", "/clear", "/supervisor", "/mode", "/mode silent", "/mode standard",
             "/model", "/model qwen35", "/model qwen36", "/pref"]
_completer = WordCompleter(
    _COMMANDS,
    meta_dict={
        "/exit": "退出",
        "/clear": "清空历史",
        "/supervisor": "切换 supervisor (simple/milestone)",
        "/pref": "查看/设置偏好 (set 外卖 美团 / del 外卖)",
    },
)
_pt_prompt = ANSI("\033[1;36m❯ \033[0m")


def _handle_pref(cmd: str, prefs: PreferenceManager) -> None:
    parts = cmd.split()
    if len(parts) == 1:
        all_prefs = prefs.list_app_prefs()
        if not all_prefs:
            console.print("  [dim]暂无偏好设置[/dim]")
        else:
            for p in all_prefs:
                src = "手动" if p.source == "manual" else "自动"
                console.print(f"  {p.intent} → {p.app}  [dim][{src}][/dim]")
        console.print()
        return
    if parts[1] == "set" and len(parts) == 4:
        prefs.set_app_pref(parts[2], parts[3], source="manual")
        console.print(f"  [green]已设置: {parts[2]} → {parts[3]}[/green]")
        console.print()
        return
    if parts[1] == "del" and len(parts) == 3:
        if prefs.remove_app_pref(parts[2]):
            console.print(f"  [green]已删除: {parts[2]}[/green]")
        else:
            console.print(f"  [yellow]未找到: {parts[2]}[/yellow]")
        console.print()
        return
    console.print("  [dim]用法: /pref | /pref set 外卖 美团 | /pref del 外卖[/dim]")
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lucas 多轮对话")
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()

    action_policy = build_policy(StructuredOutputPolicy.name)
    supervisor = build_supervisor(MilestoneSupervisorPolicy.name)
    prefs = PreferenceManager()
    _MODE_BACKEND = {"silent": "daemon", "standard": "mirroir"}
    _env_default = "standard" if os.environ.get("AGENT_MODE", "silent").lower() in ("mirroir", "standard") else "silent"
    # /model reads AGENT_MODEL at startup; switch_config already handles it via _active_config_path
    mode: str = _env_default

    SESSIONS_ROOT = ROOT / "data" / "sessions"
    _print_header()
    session: list[dict] = []
    recorder = SessionRecorder(
        SESSIONS_ROOT,
        supervisor=supervisor.name,
        action_policy=action_policy.name,
    )
    _pending_clarification_msg: str | None = None  # original msg that triggered clarification

    while True:
        try:
            user_msg = pt_prompt(_pt_prompt, completer=_completer).strip()
        except (EOFError, KeyboardInterrupt):
            recorder.save()
            console.print()
            break

        if not user_msg:
            continue

        if user_msg == "/exit":
            recorder.save()
            console.print("[dim]再见[/dim]")
            break

        if user_msg == "/clear":
            recorder.save()
            recorder = SessionRecorder(
                SESSIONS_ROOT,
                supervisor=supervisor.name,
                action_policy=action_policy.name,
            )
            session.clear()
            _pending_clarification_msg = None
            console.clear()
            _print_header()
            continue

        if user_msg == "/supervisor":
            current = supervisor.name
            if current == SimpleSupervisorPolicy.name:
                supervisor = build_supervisor(MilestoneSupervisorPolicy.name)
            else:
                supervisor = build_supervisor(SimpleSupervisorPolicy.name)
            console.print()
            console.print(f"  [dim]supervisor: {current} → {supervisor.name}[/dim]")
            console.print()
            continue

        if user_msg == "/mode" or user_msg.startswith("/mode "):
            parts = user_msg.split()
            if len(parts) >= 2 and parts[1] in _MODE_BACKEND:
                mode = parts[1]
            else:
                mode = "standard" if mode == "silent" else "silent"
            console.print()
            desc = "零抢占 mirror_daemon" if mode == "silent" else "mirroir-mcp 原版"
            console.print(f"  [dim]mode: {mode}  ({desc})[/dim]")
            console.print()
            continue

        if user_msg == "/model" or user_msg.startswith("/model "):
            from policy_expr.config import switch_config, active_config_name, _NAMED_CONFIGS
            parts = user_msg.split()
            _MODELS = list(_NAMED_CONFIGS.keys())
            if len(parts) >= 2 and parts[1] in _MODELS:
                model_name = parts[1]
            else:
                current = active_config_name()
                model_name = next(m for m in _MODELS if m != current)
            path = switch_config(model_name)
            console.print()
            console.print(f"  [dim]model: {model_name}  ({path.name})[/dim]")
            console.print()
            continue

        if user_msg.startswith("/pref"):
            _handle_pref(user_msg, prefs)
            continue

        # Route — if answering a clarification, merge original msg with this answer
        route_msg = user_msg
        display_msg = user_msg  # what gets stored in session as user_msg
        from_clarification = False
        if _pending_clarification_msg and user_msg not in _COMMANDS:
            route_msg = f"{_pending_clarification_msg}（补充说明：{user_msg}）"
            display_msg = route_msg
            _pending_clarification_msg = None
            from_clarification = True

        try:
            prefs_context = prefs.format_prefs_for_prompt()
            router_result = route_message(route_msg, session, prefs_context=prefs_context)
        except Exception:
            router_result = RouterResult(goal=route_msg)

        # Clarification answer is an explicit preference signal — extract immediately
        # regardless of whether the task will succeed later.
        if from_clarification and router_result.goal:
            prefs.auto_extract(display_msg, router_result.goal, session)

        if not router_result.goal:
            if router_result.needs_clarification:
                _pending_clarification_msg = route_msg
                console.print()
                console.print(
                    Panel(
                        Text(router_result.clarification, style="yellow"),
                        border_style="yellow",
                        box=ROUNDED,
                        padding=(0, 2),
                    )
                )
                console.print()
                recorder.add({
                    "user_msg": display_msg,
                    "clarification": router_result.clarification,
                    "stop_reason": "需要补充信息",
                    "goal_completed": False,
                    "turns_count": 0,
                })
            else:
                t_reply = time.time()
                reply_state: dict = {"current": "正在生成回复…", "done": False}
                with Live(
                    _SpinnerLine(reply_state),
                    console=console,
                    refresh_per_second=10,
                    transient=False,
                ):
                    reply = generate_reply(user_msg, None, session=session)
                reply_secs = time.time() - t_reply
                reply_state["done"] = True
                reply_state["current"] = f"回复生成完成  {reply_secs:.1f}s"
                _print_reply(reply)
                entry = {
                    "user_msg": user_msg,
                    "reply": reply,
                    "result_summary": reply,
                    "stop_reason": "非手机操作",
                    "goal_completed": False,
                    "turns_count": 0,
                }
                session.append(entry)
                recorder.add(entry)
            continue

        # Execute
        goal = router_result.goal or user_msg
        turn_supervisor = build_supervisor(supervisor.name)

        knowledge = auto_discover_knowledge(goal)
        if knowledge and hasattr(turn_supervisor, "set_app_knowledge"):
            turn_supervisor.set_app_knowledge(
                knowledge.navigation,
                app_name=knowledge.app_name,
                elements=knowledge.elements,
            )

        log_dir = recorder.next_turn_dir()

        t0 = time.time()
        live_state: dict = {"current": "连接中...", "done": False}

        with Live(
            _SpinnerLine(live_state),
            console=console,
            refresh_per_second=10,
            transient=False,
        ) as live:
            def _on_turn(entry: dict) -> None:
                live.console.print(_turn_line(entry))

            try:
                result = run_chat_turn(
                    goal, action_policy, turn_supervisor, log_dir,
                    max_turns=args.max_turns, live_state=live_state,
                    backend=_MODE_BACKEND[mode],
                    on_turn=_on_turn,
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
            exec_secs = time.time() - t0
            live_state["done"] = True
            live_state["current"] = f"执行完成  {exec_secs:.1f}s"

        _print_result(result)

        t1 = time.time()
        reply_state: dict = {"current": "正在生成回复…", "done": False}
        with Live(
            _SpinnerLine(reply_state),
            console=console,
            refresh_per_second=10,
            transient=False,
        ):
            reply = generate_reply(
                user_msg, result, session=session,
                content_notes=result.get("content_notes"),
                collection_context=result.get("collection_context"),
            )
            reply_secs = time.time() - t1
            reply_state["done"] = True
            reply_state["current"] = f"回复生成完成  {reply_secs:.1f}s"

        _print_reply(reply)

        entry = {
            "user_msg": display_msg,
            "goal": goal,
            "reply": reply,
            "result_summary": result["result_summary"],
            "stop_reason": result["stop_reason"],
            "goal_completed": result["goal_completed"],
            "turns_count": result["turns_count"],
            "log_dir": str(log_dir.relative_to(ROOT)),
        }
        session.append(entry)
        recorder.add(entry)

        if result["goal_completed"] and goal:
            prefs.auto_extract(display_msg, goal, session)


if __name__ == "__main__":
    main()
