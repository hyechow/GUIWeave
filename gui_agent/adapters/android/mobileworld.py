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
             start state) + GET /task/goal (the intent) — in the ``_prime`` hook.
  run      : decompose the goal into the DSL orchestrator program, then run_agent_loop
             drives each statement over adb.
  post-run : (optional) POST /step answer to set the backend's interaction_cache for
             answer-style tasks + GET /task/eval (score, reason) + POST /task/tear_down.

The agent reaches BOTH the emulator (adb, e.g. host:5556) and the backend (HTTP,
host:6800). On the MobileWorld Docker host those container ports must be reachable
from this machine (a tiny adb relay + portproxy for 5556, a portproxy for 6800).

Usage:
  AGENT_PLATFORM is forced to "android" here; ANDROID_SERIAL is set from --adb-serial.
  uv run python -m gui_agent.adapters.android.mobileworld <task_name> \
      --base-url http://192.168.31.57:6800 --adb-serial 192.168.31.57:5556
  # discover task names:
  uv run python -m gui_agent.adapters.android.mobileworld --list \
      --base-url http://192.168.31.57:6800

Each turn observes the current frame; ordinary semantic Interact scrolling remains
available.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from gui_agent.core.run.result import AgentResult, failed_result

# Tags that mark non-GUI-only task subsets; excluded from the default task list (the
# GUI-only subset is the integration target — see the MobileWorld paper).
_NON_GUI_TAGS = ("agent-mcp", "agent-user-interaction")


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


def _final_answer(result: AgentResult) -> str:
    """Return the Program's user-facing output for answer-style tasks."""
    return result.output.strip()


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a MobileWorld task on the android agent loop")
    parser.add_argument("task", nargs="?", help="MobileWorld task name (omit with --list)")
    parser.add_argument("--base-url", default=os.environ.get("MW_BASE_URL", "http://192.168.31.57:6800"),
                        help="MobileWorld backend URL (env MW_BASE_URL; default :6800)")
    parser.add_argument("--adb-serial", default=os.environ.get("MW_ADB_SERIAL")
                        or os.environ.get("ANDROID_SERIAL", "192.168.31.57:5556"),
                        help="adb serial for the emulator (env MW_ADB_SERIAL/ANDROID_SERIAL; default :5556)")
    parser.add_argument("--device", default="emulator-5554",
                        help="in-container emulator serial the backend controls (default emulator-5554)")
    parser.add_argument("--list", action="store_true", help="list GUI-only task names and exit")
    parser.add_argument("--all-tasks", action="store_true", help="with --list, include non-GUI (mcp/user-interaction) tasks")
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--headless", action="store_true", help="run fully headless (no HUD / cursor overlay)")
    parser.add_argument("--no-teardown", action="store_true", help="skip /task/tear_down (leave app state for inspection)")
    parser.add_argument("--no-answer-bridge", action="store_true", help="do not POST a final answer to the backend before eval")
    args = parser.parse_args()

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
    hud = build_platform().make_status_reporter(not args.headless)
    log_dir = create_run_dir("mobileworld", "android")
    print(f"[mobileworld] agent logs: {log_dir}")

    with tee_stdio(log_dir):
        # Bind app knowledge by the task's declared apps (best-effort; none → run bare).
        from gui_agent.core.self_learning.app_summary import load_knowledge_for_app

        knowledge = None
        knowledge_summary: Optional[dict] = None
        try:
            apps = env.metadata(args.task).get("apps") or []
        except Exception as exc:  # noqa: BLE001
            apps = []
            print(f"[mobileworld] metadata unavailable ({exc}); running bare")
        for app in apps:
            knowledge = load_knowledge_for_app(app, "android")
            if knowledge and knowledge.navigation and hasattr(supervisor, "set_app_knowledge"):
                supervisor.set_app_knowledge(
                    knowledge.navigation,
                    app_name=knowledge.app_name,
                    elements=knowledge.elements,
                    sections=knowledge.sections,
                    check=knowledge.check,
                )
                knowledge_summary = knowledge.summary()
                print(f"[mobileworld] knowledge: bound app={app} "
                      f"(nav={knowledge_summary['nav_chars']} chars, "
                      f"sections={knowledge_summary['section_count']})")
                break
        else:
            if apps:
                print(f"[mobileworld] knowledge: none for apps={apps} — running bare")

        def _prime(_platform) -> None:
            # Reset the app to the task's start state via the backend (the only HTTP we
            # need pre-run; actions thereafter go over adb).
            print(f"[mobileworld] init_task {args.task} (resetting app state)...")
            env.init_task(args.task)
            print("[mobileworld] init_task OK")

        result: AgentResult | None = None
        try:
            bundle = build_platform()
            setup = bundle.setup_check()
            for line in setup.lines:
                print(line)
            if not setup.ok:
                result = failed_result(
                    goal,
                    f"环境检查未通过：{setup.summary}",
                    task_type="RETRIEVE",
                    failure_kind="environment",
                )
            else:
                orchestrator_context_reports: list[dict] = []
                with bundle.open_session() as platform:
                    _prime(platform)

                    def _compile_program():
                        run_max_turns = args.max_turns
                        cur_site = knowledge.app_name if knowledge is not None else ""

                        from gui_agent.core.orchestrator import (
                            generate_reviewed_code,
                            program_from_plan,
                        )
                        from gui_agent.core.supervisor.statement.model_io import resolve_file_refs
                        from gui_agent.core.router import resolve_intent

                        file_section = resolve_file_refs(intent)
                        resolution = resolve_intent(intent)
                        if resolution.entities:
                            print("[mobileworld] intent: " + "; ".join(
                                f"{e.mention}→{e.type}/{e.match_mode}/key={e.search_key}" for e in resolution.entities))
                        plan = generate_reviewed_code(
                            intent,
                            knowledge=knowledge.decompose_context(intent) if knowledge else "",
                            file_section=file_section,
                            current_url="",
                            current_title="",
                            current_site=cur_site,
                            resolution=resolution,
                        )
                        orchestrator_context_reports.append({
                            "kind": "coding_review",
                            "source": plan.source,
                            "approved": bool(plan.review and plan.review.approved),
                            "issues": [
                                issue.render()
                                for issue in (
                                    plan.review.issues if plan.review else ()
                                )
                            ],
                            "error": plan.review.error if plan.review else "",
                            "degraded": bool(
                                plan.review and plan.review.unavailable
                            ),
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
                            raw_input=intent,
                            router=None,
                            knowledge=knowledge_summary,
                            program=program,
                            orchestrator_context_reports=orchestrator_context_reports,
                            stop_requested=esc_stop.requested if esc_stop.enabled else None,
                            platform=platform,
                        )

            # ----- post-run: answer bridge + official state-based eval -----
            if result is None:
                raise RuntimeError("MobileWorld run ended without AgentResult")
            if not args.no_answer_bridge:
                answer = _final_answer(result)
                if answer:
                    try:
                        env.answer(answer)
                        print(f"[mobileworld] answer submitted ({len(answer)} chars)")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[mobileworld] answer bridge skipped ({exc})")

            score: float | None = None
            reason: str | None = None
            try:
                score, reason = env.eval(args.task)
                print(f"[mobileworld] OFFICIAL_EVAL score={score} reason={reason!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"[mobileworld] eval failed ({exc})")

            if not args.no_teardown:
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
