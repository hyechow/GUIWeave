"""Multi-turn conversational CLI for iPhone automation agent."""

from __future__ import annotations

import json
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv()

from rich.box import ROUNDED
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from gui_agent.core.self_learning.app_summary import (
    auto_discover_knowledge,
    load_knowledge_for_app,
    match_app_by_url,
)
from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.run.io import TeeStream as _TeeStream
from gui_agent.core.runner import (
    build_policy,
    build_supervisor,
    run_agent_loop,
)
from gui_agent.core.run.state import write_final_program_outcome
from gui_agent.core.run.result import failed_result
from gui_agent.core.chat.session import (
    RouterResult,
    generate_reply,
    route_message,
)
from gui_agent.core.chat.prefs import PreferenceManager
from gui_agent.core.chat.recorder import SessionRecorder

ROOT = Path(__file__).resolve().parents[3]

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
        # redirect_stderr only swaps Python's sys.stderr; native/Cocoa code (e.g. the
        # AppKit "IMKCFRunLoopWakeUpReliable" warning) writes straight to OS fd 2 and
        # would leak onto the Rich UI. Redirect fd 2 to the log too. fd 1 is left alone
        # so the spinner (Rich console, captured original stdout) still renders.
        saved_fd2 = os.dup(2)
        try:
            os.dup2(stderr_file.fileno(), 2)
            yield
        except Exception:
            traceback.print_exc()
            raise
        finally:
            os.dup2(saved_fd2, 2)
            os.close(saved_fd2)


def run_chat_turn(
    goal: str,
    action_policy,
    supervisor,
    log_dir: Path,
    max_turns: int = 20,
    live_state: dict | None = None,
    backend: str = "daemon",
    on_turn: object = None,
    raw_input: str | None = None,
    router: dict | None = None,
    knowledge: dict | None = None,
    decompose_knowledge: str = "",
    current_url: str = "",
    current_title: str = "",
    current_site: str = "",
) -> dict:
    """Thin wrapper around run_agent_loop with silent stdio, HUD and live_state spinner."""
    context_path = log_dir / "context.json"
    from gui_agent.core.orchestrator import decompose
    from gui_agent.core.router import resolve_intent

    reports: list[dict] = []
    resolution = resolve_intent(goal)
    program = decompose(
        goal,
        knowledge=decompose_knowledge,
        current_url=current_url,
        current_title=current_title,
        current_site=current_site,
        context_reports=reports,
        resolution=resolution,
    )
    # HUD comes from the platform bundle (make_status_reporter(True) -> the HUD
    # context manager); same object the standalone runner uses, no adapter import.
    with _silent_stdio(log_dir), build_platform().make_status_reporter(True) as hud:
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
            raw_input=raw_input,
            router=router,
            knowledge=knowledge,
            program=program,
            orchestrator_context_reports=reports,
        )


_BROWSER_PAGE_CONTEXT_HINTS = (
    "当前", "这里", "本页", "此页", "这个页面", "该页面", "当前页面", "当前页",
    "当前站点", "当前网站", "这个站点", "这个网站", "本网站", "前台",
)

_BROWSER_TASK_HINTS = (
    "打开", "进入", "点击", "搜索", "查", "查看", "查询", "创建", "新建", "新增",
    "编辑", "修改", "删除", "上传", "下载", "登录", "退出", "筛选", "过滤",
    "导出", "提交", "保存", "填写", "输入", "选择", "下单", "订单", "页面",
    "站点", "网站", "后台", "列表", "表单", "评论", "review",
)


def _should_probe_browser_page(text: str, *, from_clarification: bool = False) -> bool:
    """Cheap pre-router gate for browser page context.

    Avoid reconnecting/screenshotting the browser for casual chat. Probe only when
    the user refers to the current page/site or the utterance looks like a browser
    operation/retrieval task. Clarification answers are probed because they often
    complete a previously routed browser request.
    """
    s = (text or "").strip().lower()
    if not s:
        return False
    if from_clarification:
        return True
    return any(h in s for h in _BROWSER_PAGE_CONTEXT_HINTS) or any(h in s for h in _BROWSER_TASK_HINTS)


def _probe_current_browser_page(bundle) -> tuple[str, str, str]:
    """Best-effort browser front-tab identity for chat routing.

    Keep this intentionally light: read Chrome CDP's /json/list active page target
    directly. Do not connect Playwright or take a screenshot here; chat routing is
    on the critical path and many messages do not need browser state at all.
    """
    if bundle.platform != "browser":
        return "", "", ""
    try:
        import json
        from urllib.request import urlopen

        cdp_url = os.environ.get("CHROME_CDP_URL") or "http://localhost:9222"
        raw = urlopen(cdp_url.rstrip("/") + "/json/list", timeout=0.8).read()
        targets = json.loads(raw.decode("utf-8"))
        # Pick the active target the SAME way PlaywrightDevice._active_page_from_json_list
        # does (first type==page with an id) so chat routing and the device bind agree on
        # which tab is "active". A blank active tab then yields url="" (no context) rather
        # than reaching past it for some other tab's url.
        active = next((t for t in targets if t.get("type") == "page" and t.get("id")), None)
        if not active:
            return "", "", ""
        url = active.get("url") or ""
        title = active.get("title") or ""
        site = match_app_by_url(url, bundle.platform) if url else ""
        return url, title, site or ""
    except Exception:
        return "", "", ""


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
    console.print("  [dim]/exit  退出  ·  /clear  清空历史[/]")
    console.print("  [dim]/mode [silent|standard]  切换操作模式  ·  /model [qwen35|qwen36]  切换模型  ·  /max-turns <n>  最大轮数[/]")
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
    phase = result.get("phase") or "stopped"
    verification = result.get("verification")
    confirmed = phase == "completed" and verification == "confirmed"
    accepted = phase == "completed" and verification == "accepted_unverified"
    color = "green" if confirmed else "yellow" if accepted else "red"
    border = color
    icon = "✓" if confirmed else "~" if accepted else "✗"
    label = "done" if confirmed else "accepted" if accepted else phase

    turns = result.get("turns_count", 0)
    suffix = f"  [dim]{turns} turns[/dim]" if turns else ""
    tree = Tree(f"[bold {color}]{icon}  {result['output']}[/bold {color}]{suffix}")

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


_COMMANDS = ["/exit", "/clear", "/mode", "/mode silent", "/mode standard",
             "/model", "/model qwen35", "/model qwen36", "/max-turns", "/pref"]
_completer = WordCompleter(
    _COMMANDS,
    meta_dict={
        "/exit": "退出",
        "/clear": "清空历史",
        "/max-turns": "设置单任务最大轮数 (/max-turns 30)",
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
    # Default policy/supervisor names come from the platform bundle (no adapter
    # import); they resolve to "structured_output" / "statement" as before.
    bundle = build_platform()
    action_policy = build_policy(bundle.default_action_policy)
    supervisor = build_supervisor(bundle.default_supervisor)
    prefs = PreferenceManager()
    _MODE_BACKEND = {"silent": "daemon", "standard": "mirroir"}
    _env_default = "standard" if os.environ.get("AGENT_MODE", "silent").lower() in ("mirroir", "standard") else "silent"
    # /model switches the active profile at runtime (AGENT_MODEL sets the startup default)
    mode: str = _env_default
    max_turns: int = 25   # 默认 25；运行时用 /max-turns 调整

    SESSIONS_ROOT = ROOT / "data" / "sessions"
    _print_header()
    session: list[dict] = []
    recorder = SessionRecorder(
        SESSIONS_ROOT,
        supervisor=supervisor.name,
        action_policy=action_policy.name,
        platform=bundle.platform,
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
            from gui_agent.core.config import switch_config, active_config_name, available_profiles
            parts = user_msg.split()
            _MODELS = available_profiles()
            if len(parts) >= 2 and parts[1] in _MODELS:
                model_name = parts[1]
            else:
                current = active_config_name()
                model_name = next((m for m in _MODELS if m != current), current)
            switch_config(model_name)
            console.print()
            console.print(f"  [dim]model profile: {model_name}[/dim]")
            console.print()
            continue

        if user_msg == "/max-turns" or user_msg.startswith("/max-turns "):
            parts = user_msg.split()
            console.print()
            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) > 0:
                max_turns = int(parts[1])
                console.print(f"  [dim]max_turns: {max_turns}[/dim]")
            else:
                console.print(f"  [dim]当前 max_turns: {max_turns}  ·  用法: /max-turns <正整数>[/dim]")
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

        cur_url = cur_title = cur_site = ""
        if _should_probe_browser_page(route_msg, from_clarification=from_clarification):
            cur_url, cur_title, cur_site = _probe_current_browser_page(bundle)
        try:
            prefs_context = prefs.format_prefs_for_prompt()
            router_result = route_message(
                route_msg,
                session,
                prefs_context=prefs_context,
                platform=bundle.platform,
                current_url=cur_url,
                current_title=cur_title,
                current_site=cur_site,
            )
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
                    "summary": "需要补充信息",
                    "phase": "stopped",
                    "verification": None,
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
                    "output": reply,
                    "summary": "非手机操作",
                    "phase": "stopped",
                    "verification": None,
                    "turns_count": 0,
                }
                session.append(entry)
                recorder.add(entry)
            continue

        # Execute
        goal = router_result.goal or user_msg
        turn_supervisor = build_supervisor(supervisor.name)

        knowledge = auto_discover_knowledge(goal, bundle.platform)
        if knowledge is None and cur_site:
            knowledge = load_knowledge_for_app(cur_site, bundle.platform)
        knowledge_summary: dict | None = None
        if knowledge and hasattr(turn_supervisor, "set_app_knowledge"):
            turn_supervisor.set_app_knowledge(
                knowledge.navigation,
                app_name=knowledge.app_name,
                elements=knowledge.elements,
                sections=knowledge.sections,
                check=knowledge.check,
            )
            knowledge_summary = knowledge.summary()

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
                    max_turns=max_turns, live_state=live_state,
                    backend=_MODE_BACKEND[mode],
                    on_turn=_on_turn,
                    raw_input=display_msg,
                    router=router_result.model_dump(),
                    knowledge=knowledge_summary,
                    decompose_knowledge=(knowledge.decompose_context(goal) if knowledge else ""),
                    current_url=cur_url,
                    current_title=cur_title,
                    current_site=cur_site,
                )
            except (SystemExit, KeyboardInterrupt):
                raise
            except Exception as exc:
                result = failed_result(goal, f"异常: {exc}").model_dump(mode="json")
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

        # Persist the reply into the turn's context.json so its report (bin/report) shows it.
        try:
            ctx_path = log_dir / "context.json"
            if ctx_path.exists():
                write_final_program_outcome(ctx_path, result, reply)
        except Exception:
            pass

        entry = {
            "user_msg": display_msg,
            "goal": goal,
            "reply": reply,
            "output": result["output"],
            "summary": result["summary"],
            "phase": result["phase"],
            "verification": result.get("verification"),
            "turns_count": result["turns_count"],
            "log_dir": str(log_dir.relative_to(ROOT)),
        }
        session.append(entry)
        recorder.add(entry)

        if (
            result["phase"] == "completed"
            and result.get("verification") == "confirmed"
            and goal
        ):
            prefs.auto_extract(display_msg, goal, session)


if __name__ == "__main__":
    main()
