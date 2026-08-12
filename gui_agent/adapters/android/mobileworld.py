"""MobileWorld entry — runs a MobileWorld task on the existing android agent loop.

MobileWorld (Tongyi/Alibaba, the Android analog of webarena-verified) ships a Docker
env: a headless rooted emulator + a FastAPI backend (default port 6800) that owns task
lifecycle and **state-based grading** (``/task/eval`` → ``task.is_successful(ctr)``
inspects the emulator via adb; it does NOT replay or inspect the agent's action trace).

Because grading is pure system state, HOW the actions reach the emulator is irrelevant
to the score. So this entry is THIN and reuses the real android agent (perception +
statement supervisor + executor + visualizer) by driving the emulator over **adb**
(the existing :class:`AndroidDevice`, e.g. ``adb connect <host>:5556``) — which is more
capable than the backend's ``/step`` (it has clear_text / keycodes / amount-aware
scroll). MobileWorld's HTTP API is used ONLY for the task lifecycle:

  pre-run  : POST /init (controller) + POST /task/init (reset the app to the task's
             start state), wait for external adb to recover, then open a fresh
             Android session; GET /task/goal supplies the intent.
  run      : execute either a reviewed Python program or Tool Agent's reviewed
             Master + autonomous visual Workers; both drive Android over adb.
  post-run : (optional) POST /step answer to set the backend's interaction_cache for
             answer-style tasks + GET /task/eval (score, reason) + POST /task/tear_down.

The agent reaches BOTH the emulator (adb, e.g. host:5556) and the backend (HTTP,
host:6800). On the MobileWorld Docker host those container ports must be reachable
from this machine (a tiny adb relay + portproxy for 5556, a portproxy for 6800).

Usage:
  AGENT_PLATFORM is forced to "android" here; ANDROID_SERIAL is set from --adb-serial.
  uv run python -m gui_agent.adapters.android.mobileworld <task_name> \
      --base-url http://192.168.1.103:6800 --adb-serial 192.168.1.103:5556
  # discover task names:
  uv run python -m gui_agent.adapters.android.mobileworld --list \
      --base-url http://192.168.1.103:6800

Each turn observes the current frame; ordinary semantic Interact scrolling remains
available.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from gui_agent.core.run.result import AgentResult, failed_result

# Tags that mark non-GUI-only task subsets; excluded from the default task list (the
# GUI-only subset is the integration target — see the MobileWorld paper).
_NON_GUI_TAGS = ("agent-mcp", "agent-user-interaction")

# MobileWorld owns a fixed emulator image, so its package names are entry-point facts.
MOBILEWORLD_PACKAGE_MANAGER: dict[str, str] = {
    "Calendar": "org.fossify.calendar",
    "Camera": "com.android.camera2",
    "Chrome": "com.android.chrome",
    "Clock": "com.google.android.deskclock",
    "Contacts": "com.google.android.contacts",
    "Docreader": "at.tomtasche.reader",
    "Files": "com.google.android.documentsui",
    "Gallery": "gallery.photomanager.picturegalleryapp.imagegallery",
    "Mail": "com.gmailclone",
    "Maps": "com.google.android.apps.maps",
    "Mastodon": "org.joinmastodon.android.mastodon",
    "Mattermost": "com.mattermost.rnbeta",
    "Messages": "com.google.android.apps.messaging",
    "Settings": "com.android.settings",
    "Taodian": "com.testmall.app",
}


class MobileWorldEnv:
    """Thin HTTP client for MobileWorld's backend — task lifecycle + grading only.

    A deliberately small subset of MobileWorld's own ``AndroidEnvClient`` (we talk to
    the same FastAPI server directly via ``requests``, without importing their package):
    we do NOT route actions or screenshots through it — the agent drives the emulator
    over adb. ``device`` is the IN-CONTAINER emulator serial the backend controls
    (``emulator-5554``), distinct from the adb serial this machine connects to.
    """

    def __init__(self, base_url: str, *, device: str = "emulator-5554", timeout: float = 30.0):
        import requests

        self.base_url = base_url.rstrip("/")
        self.device = device
        self.timeout = timeout
        self._initialized = False
        # The MobileWorld backend is always a LAN/local host — never reached through an
        # HTTP proxy. trust_env=False makes this session ignore http_proxy/https_proxy
        # (which would otherwise 502 a LAN host), WITHOUT touching the agent's own LLM
        # client proxy usage (that runs through different code).
        self._session = requests.Session()
        self._session.trust_env = False

    def _req(self, method: str, path: str, *, timeout: float | None = None, **kwargs):
        resp = self._session.request(
            method, f"{self.base_url}{path}", timeout=timeout or self.timeout, **kwargs
        )
        resp.raise_for_status()
        return resp

    def ensure_init(self) -> None:
        """Initialize the backend controller for this emulator (idempotent)."""
        if self._initialized:
            return
        self._req("POST", "/init", json={"device": self.device})
        self._initialized = True

    def health(self) -> bool:
        try:
            return bool(self._req("GET", "/health").json().get("ok", False))
        except Exception:  # noqa: BLE001
            return False

    def task_list(self, *, gui_only: bool = True) -> list[str]:
        """All task names; by default filtered to the GUI-only subset (drops mcp /
        user-interaction tagged tasks, matching MobileWorld's default suite)."""
        self.ensure_init()
        tasks = self._req("GET", "/task/list").json()
        names: list[str] = []
        for task in tasks:
            tags = task.get("tags", []) if isinstance(task, dict) else []
            if gui_only and any(t in tags for t in _NON_GUI_TAGS):
                continue
            names.append(task["name"] if isinstance(task, dict) else task)
        return names

    def metadata(self, task_name: str) -> dict:
        self.ensure_init()
        return self._req("GET", "/task/metadata", params={"task_name": task_name}).json()

    def get_goal(self, task_name: str) -> str:
        self.ensure_init()
        return self._req("GET", "/task/goal", params={"task_name": task_name}).json()

    def init_task(self, task_name: str) -> None:
        """Reset the app to the task's start state (the backend's setup hooks)."""
        self.ensure_init()
        self._req(
            "POST", "/task/init",
            json={"task_name": task_name, "req_device": self.device}, timeout=300,
        )

    def answer(self, text: str) -> None:
        """Submit a textual answer via /step (sets the backend's interaction_cache,
        which answer-style tasks grade against). Best-effort: drive-by for tasks that
        need it; harmless for state-only tasks."""
        self.ensure_init()
        self._req(
            "POST", "/step",
            json={"device": self.device, "action": {"action_type": "answer", "text": text}},
        )

    def eval(self, task_name: str) -> tuple[float, str]:
        self.ensure_init()
        # MobileWorld reads the request body on a GET here (matches its own client).
        result = self._req(
            "GET", "/task/eval",
            json={"task_name": task_name, "req_device": self.device},
        ).json()
        score = float(result.get("score", 0.0))
        reason = result.get("reason", "")
        return score, reason

    def tear_down(self, task_name: str) -> None:
        self.ensure_init()
        self._req(
            "POST", "/task/tear_down",
            json={"task_name": task_name, "req_device": self.device},
        )


def _init_task_then_wait_for_android(
    env: MobileWorldEnv,
    task_name: str,
    setup_check: Callable[[], object],
    *,
    ready_timeout_s: float = 120.0,
    poll_s: float = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
):
    """Reset task state before opening an adb session, then wait for adb readiness.

    MobileWorld task initialization may restart or temporarily disconnect the
    emulator.  Opening :class:`AndroidSession` first leaves the run holding a stale
    adbutils device handle.  This lifecycle boundary deliberately completes the
    backend reset first and only lets the caller open a fresh session after the
    platform setup check reports a live ``device`` transport.
    """
    print(f"[mobileworld] init_task {task_name} (resetting app state)...")
    env.init_task(task_name)
    print("[mobileworld] init_task OK; waiting for external adb...")

    deadline = monotonic() + max(0.0, ready_timeout_s)
    attempt = 0
    while True:
        attempt += 1
        setup = setup_check()
        if getattr(setup, "ok", False):
            if attempt > 1:
                print(f"[mobileworld] external adb ready after {attempt} checks")
            return setup
        remaining = deadline - monotonic()
        if remaining <= 0:
            return setup
        summary = getattr(setup, "summary", "android environment unavailable")
        print(
            f"[mobileworld] adb not ready ({summary}); "
            f"retrying in {min(poll_s, remaining):.1f}s"
        )
        sleep(min(poll_s, remaining))


def _final_answer(result: AgentResult) -> str:
    """Return the Program's exact output for MobileWorld answer-style grading."""
    return result.output.strip()


def _route_mobileworld_goal(
    goal: str,
    *,
    route: Callable[..., object] | None = None,
) -> tuple[str, dict]:
    """Normalize one backend goal while preserving it as the raw evaluator input."""
    if route is None:
        from gui_agent.core.chat.session import route_message

        route = route_message
    result = route(goal, session=[], platform="android")
    routed_goal = str(getattr(result, "goal", "") or "").strip()
    payload = result.model_dump(mode="json")
    return routed_goal or goal, payload


def _android_platform_contract(app_names: list[str]) -> str:
    """Render Android capabilities against exact semantic application names."""
    from gui_agent.prompts import load_prompt

    return load_prompt("task.orchestrator.android").render(
        app_list=json.dumps(app_names, ensure_ascii=False)
    )


def _generate_and_persist_reply(
    context_path: Path,
    goal: str,
    result: AgentResult,
) -> str:
    """Generate the frontend reply without changing the evaluator-facing output."""
    from gui_agent.core.llm.output import generate_reply
    from gui_agent.core.run.state import write_final_reply

    reply = generate_reply(goal, result.model_dump(mode="json"))
    write_final_reply(context_path, reply)
    return reply


def _write_mobileworld_context(
    context_path: Path,
    *,
    task_name: str,
    goal: str,
    base_url: str,
    adb_serial: str,
    score: float | None,
    reason: str | None,
) -> None:
    """Patch context.json with the MobileWorld task + verdict (so report.html carries it)."""
    if not context_path.exists():
        return
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    raw["mobileworld"] = {
        "task_name": task_name,
        "goal": goal,
        "base_url": base_url,
        "adb_serial": adb_serial,
        "score": score,
        "reason": reason,
    }
    context_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def _guess_task_type(goal: str) -> str:
    """Best-effort report semantics when Tool Agent fails before selecting an effect."""

    text = goal.strip().lower()
    mutate_markers = (
        "create", "add", "edit", "update", "change", "delete", "remove",
        "set ", "submit", "place", "enable", "disable", "assign", "save",
        "mark", "rename", "notify", "send", "turn on", "turn off",
    )
    navigate_markers = ("open ", "go to", "navigate", "visit")
    if any(marker in text for marker in mutate_markers):
        return "MUTATE"
    if any(marker in text for marker in navigate_markers):
        return "NAVIGATE"
    return "RETRIEVE"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a MobileWorld task on the android agent loop")
    parser.add_argument("task", nargs="?", help="MobileWorld task name (omit with --list)")
    parser.add_argument("--base-url", default=os.environ.get("MW_BASE_URL", "http://192.168.1.103:6800"),
                        help="MobileWorld backend URL (env MW_BASE_URL; default :6800)")
    parser.add_argument("--adb-serial", default=os.environ.get("MW_ADB_SERIAL")
                        or os.environ.get("ANDROID_SERIAL", "192.168.1.103:5556"),
                        help="adb serial for the emulator (env MW_ADB_SERIAL/ANDROID_SERIAL; default :5556)")
    parser.add_argument("--device", default="emulator-5554",
                        help="in-container emulator serial the backend controls (default emulator-5554)")
    parser.add_argument("--list", action="store_true", help="list GUI-only task names and exit")
    parser.add_argument("--all-tasks", action="store_true", help="with --list, include non-GUI (mcp/user-interaction) tasks")
    parser.add_argument("--max-turns", type=int, default=30,
                        help="maximum interactive turns (default 30)")
    parser.add_argument(
        "--runtime",
        choices=("reviewed-python", "tool-agent"),
        default="reviewed-python",
        help="execution runtime (default reviewed-python)",
    )
    parser.add_argument(
        "--perception",
        choices=("vision-only", "enhanced"),
        default="enhanced",
        help="tool-agent perception provider (default enhanced)",
    )
    parser.add_argument(
        "--tool-agent-multi-action",
        action="store_true",
        help="experimental ordered 1-5 action envelopes for Tool Agent Workers",
    )
    parser.add_argument(
        "--adb-ready-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for external adb after /task/init (default 120)",
    )
    parser.add_argument("--headless", action="store_true", help="run fully headless (no HUD / cursor overlay)")
    parser.add_argument("--no-teardown", action="store_true", help="skip /task/tear_down (leave app state for inspection)")
    parser.add_argument("--no-answer-bridge", action="store_true", help="do not POST a final answer to the backend before eval")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if not 1 <= args.max_turns <= 50:
        parser.error("--max-turns must be between 1 and 50")

    env = MobileWorldEnv(args.base_url, device=args.device)

    if args.list:
        names = env.task_list(gui_only=not args.all_tasks)
        print(f"[mobileworld] {len(names)} tasks ({'all' if args.all_tasks else 'GUI-only'}) @ {args.base_url}")
        for name in names:
            print(f"  {name}")
        return 0

    if not args.task:
        parser.error("a task name is required (or use --list)")

    # Force the android platform; route the session's adb at the MobileWorld emulator.
    os.environ["AGENT_PLATFORM"] = "android"
    os.environ["ANDROID_SERIAL"] = args.adb_serial
    if args.headless:
        os.environ["AGENT_HEADLESS"] = "1"

    from dotenv import load_dotenv
    load_dotenv()

    from gui_agent.core.runtime.factory import build_platform
    from gui_agent.core.run.io import EscStopSignal, create_run_dir, tee_stdio
    from gui_agent.core.runner import run_agent_loop, build_policy, build_supervisor

    if not env.health():
        print(f"[mobileworld] WARN backend /health not ok at {args.base_url} (continuing)")
    goal = env.get_goal(args.task)
    print(f"[mobileworld] task: {args.task}")
    print(f"[mobileworld] goal: {goal}")
    print(f"[mobileworld] backend: {args.base_url}   adb: {args.adb_serial}")

    intent = goal
    action_policy = build_policy("android_vision")
    supervisor = build_supervisor("statement")
    # Task initialization may restart the emulator.  Defer HUD/scrcpy construction
    # until after it finishes for the same reason we defer AndroidSession: neither
    # should retain a pre-reset adb transport.
    hud = None
    log_dir = create_run_dir("mobileworld", "android")
    print(f"[mobileworld] agent logs: {log_dir}")

    with tee_stdio(log_dir):
        router_payload: dict | None = None
        try:
            intent, router_payload = _route_mobileworld_goal(goal)
            print(f"[mobileworld] route: {goal!r} -> {intent!r}")
        except Exception as exc:  # noqa: BLE001 - preserve direct-run fallback
            print(f"[mobileworld] route unavailable; using raw goal ({exc})")

        # Bind app knowledge by the task's declared apps (best-effort; none → run bare).
        from gui_agent.core.self_learning.app_summary import load_knowledge_for_app

        app_knowledges = []
        orchestrator_knowledge = ""
        knowledge_summary: Optional[dict] = None
        try:
            declared_apps = env.metadata(args.task).get("apps") or []
        except Exception as exc:  # noqa: BLE001
            declared_apps = []
            print(f"[mobileworld] metadata unavailable ({exc}); running bare")
        for app in declared_apps:
            candidate = load_knowledge_for_app(app, "android")
            if candidate and candidate.navigation:
                app_knowledges.append(candidate)
        if app_knowledges:
            app_names = " + ".join(item.app_name for item in app_knowledges)
            orchestrator_knowledge = "\n\n".join(
                item.orchestrator_context(goal) for item in app_knowledges
            )
            if hasattr(supervisor, "set_app_knowledge"):
                supervisor.set_app_knowledge(
                    "\n\n".join(item.navigation for item in app_knowledges),
                    app_name=app_names,
                    elements="\n\n".join(item.elements for item in app_knowledges),
                    sections={
                        f"{item.app_name}:{stem}": text
                        for item in app_knowledges
                        for stem, text in item.sections.items()
                    },
                    check="\n\n".join(item.check for item in app_knowledges),
                )
            summaries = [item.summary() for item in app_knowledges]
            knowledge_summary = {"app_name": app_names, "apps": summaries}
            print(f"[mobileworld] knowledge: bound apps={app_names}")
        elif declared_apps:
            print(f"[mobileworld] knowledge: none for apps={declared_apps} — running bare")

        result: AgentResult | None = None
        tool_presentation: object | None = None
        task_initialized = False
        try:
            bundle = build_platform()
            try:
                setup = _init_task_then_wait_for_android(
                    env,
                    args.task,
                    bundle.setup_check,
                    ready_timeout_s=args.adb_ready_timeout,
                )
                task_initialized = True
            except Exception as exc:  # noqa: BLE001
                setup = None
                result = failed_result(
                    intent,
                    f"MobileWorld 任务初始化失败：{exc}",
                    task_type="RETRIEVE",
                    failure_kind="environment",
                )
                print(f"[mobileworld] init_task failed: {exc}")

            if setup is not None:
                for line in setup.lines:
                    print(line)
            if result is None and setup is not None and not setup.ok:
                result = failed_result(
                    intent,
                    f"任务初始化完成，但外部 adb 未恢复：{setup.summary}",
                    task_type="RETRIEVE",
                    failure_kind="environment",
                )
            elif result is None and setup is not None:
                orchestrator_context_reports: list[dict] = []
                hud = bundle.make_status_reporter(not args.headless)
                with bundle.open_session() as platform:
                    platform.client.package_manager = MOBILEWORLD_PACKAGE_MANAGER
                    app_list = platform.list_apps()
                    task_apps = [app for app in declared_apps if app in app_list]
                    print(f"[mobileworld] installed apps: {len(app_list)}")

                    if args.runtime == "tool-agent":
                        from gui_agent.core.tool_agent.result import (
                            execute_tool_agent,
                        )

                        print(
                            "[mobileworld][tool-agent] "
                            f"perception={args.perception} "
                            f"multi_action={args.tool_agent_multi_action}"
                        )
                        result, tool_presentation = execute_tool_agent(
                            intent=intent,
                            bundle=bundle,
                            session=platform,
                            log_dir=log_dir,
                            perception_mode=args.perception,
                            max_turns=args.max_turns,
                            allow_multi_action=args.tool_agent_multi_action,
                            fallback_task_type=_guess_task_type(intent),
                            knowledge_summary=knowledge_summary,
                            knowledge=orchestrator_knowledge,
                            hud=hud,
                            raw_input=goal,
                            router=router_payload,
                        )
                    else:
                        def _compile_program():
                            run_max_turns = args.max_turns
                            cur_site = app_names if app_knowledges else ""

                            from gui_agent.core.orchestrator import (
                                generate_code,
                                program_from_plan,
                            )
                            from gui_agent.core.supervisor.statement.model_io import resolve_file_refs
                            from gui_agent.core.router import resolve_intent

                            # File references and orchestration stay anchored to the source task;
                            # the Router may add one semantic clarification.
                            file_section = resolve_file_refs(goal)
                            resolution = resolve_intent(goal)
                            if resolution.semantic_supplement:
                                print(
                                    "[mobileworld] semantic supplement: "
                                    f"{resolution.semantic_supplement}"
                                )
                            plan = generate_code(
                                goal,
                                knowledge=orchestrator_knowledge,
                                platform_contract=_android_platform_contract(task_apps),
                                file_section=file_section,
                                current_url="",
                                current_title="",
                                current_site=cur_site,
                                resolution=resolution,
                            )
                            orchestrator_context_reports.append({
                                "kind": "coding_compile",
                                "source": plan.source,
                                "repaired": plan.repaired,
                                "events": [
                                    event.to_dict() for event in plan.events
                                ],
                            })
                            program = program_from_plan(plan)
                            if file_section:
                                cap = 3000
                                supervisor.add_static_constraint(
                                    file_section if len(file_section) <= cap
                                    else file_section[:cap] + "\n…（配置过长已截断，其余以分解结果为准）"
                                )
                            print("[mobileworld] orchestrator: reviewed Python ready")
                            return program, run_max_turns

                        program, run_max_turns = _compile_program()
                        with EscStopSignal(enabled=True) as esc_stop:
                            if esc_stop.enabled:
                                print("[mobileworld] Interrupt: 按 ESC 将在当前 turn 收尾后停止")
                            result = run_agent_loop(
                                intent,
                                action_policy,
                                supervisor,
                                None,
                                log_dir,
                                log_dir / "context.json",
                                max_turns=run_max_turns,
                                auto_continue=True,
                                hud=hud,
                                raw_input=goal,
                                router=router_payload,
                                knowledge=knowledge_summary,
                                program=program,
                                orchestrator_context_reports=orchestrator_context_reports,
                                stop_requested=esc_stop.requested if esc_stop.enabled else None,
                                platform=platform,
                            )

            # ----- post-run: answer bridge + official state-based eval -----
            if result is None:
                raise RuntimeError("MobileWorld run ended without AgentResult")
            try:
                reply = (
                    str(getattr(tool_presentation, "reply", ""))
                    if tool_presentation is not None
                    else _generate_and_persist_reply(
                        log_dir / "context.json",
                        intent,
                        result,
                    )
                )
                print("[mobileworld] FINAL_REPLY")
                print(reply)
            except Exception as exc:  # noqa: BLE001 - reply is not evaluator input
                print(f"[mobileworld] reply generation failed ({exc})")
            if task_initialized and not args.no_answer_bridge:
                answer = _final_answer(result)
                if answer:
                    try:
                        env.answer(answer)
                        print(f"[mobileworld] answer submitted ({len(answer)} chars)")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[mobileworld] answer bridge skipped ({exc})")

            score: float | None = None
            reason: str | None = None
            if task_initialized:
                try:
                    score, reason = env.eval(args.task)
                    print(f"[mobileworld] OFFICIAL_EVAL score={score} reason={reason!r}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[mobileworld] eval failed ({exc})")
            else:
                print("[mobileworld] eval skipped (task initialization did not complete)")

            if task_initialized and not args.no_teardown:
                try:
                    env.tear_down(args.task)
                    print("[mobileworld] tear_down OK")
                except Exception as exc:  # noqa: BLE001
                    print(f"[mobileworld] tear_down skipped ({exc})")

            _write_mobileworld_context(
                log_dir / "context.json",
                task_name=args.task,
                goal=goal,
                base_url=args.base_url,
                adb_serial=args.adb_serial,
                score=score,
                reason=reason,
            )

            if (log_dir / "context.json").exists():
                try:
                    from gui_agent.reports import RunnerReportBuilder, save_report
                    report_data = RunnerReportBuilder().build(log_dir)
                    report_path = save_report(report_data, log_dir / "report.html")
                    print(f"[mobileworld] OK report -> {report_path}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[mobileworld] report generation failed ({exc})")

            return 0 if (score is not None and score > 0) else 1
        finally:
            if hud:
                hud.close()


if __name__ == "__main__":
    sys.exit(main())
