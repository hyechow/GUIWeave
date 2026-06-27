"""MobileWorld entry — runs a MobileWorld task on the existing android agent loop.

MobileWorld (Tongyi/Alibaba, the Android analog of webarena-verified) ships a Docker
env: a headless rooted emulator + a FastAPI backend (default port 6800) that owns task
lifecycle and **state-based grading** (``/task/eval`` → ``task.is_successful(ctr)``
inspects the emulator via adb; it does NOT replay or inspect the agent's action trace).

Because grading is pure system state, HOW the actions reach the emulator is irrelevant
to the score. So this entry is THIN and reuses the real android agent (perception +
milestone supervisor + executor + visualizer) by driving the emulator over **adb**
(the existing :class:`AndroidDevice`, e.g. ``adb connect <host>:5556``) — which is more
capable than the backend's ``/step`` (it has clear_text / keycodes / amount-aware
scroll). MobileWorld's HTTP API is used ONLY for the task lifecycle:

  pre-run  : POST /init (controller) + POST /task/init (reset the app to the task's
             start state) + GET /task/goal (the intent) — in the ``_prime`` hook.
  run      : decompose the goal into the DSL orchestrator program, then run_agent_loop
             drives each milestone over adb; --no-orchestrator keeps the legacy DAG.
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

KNOWN LIMIT: the android adapter does not implement scroll-collect yet, so a task whose
milestone plans completion_strategy='scroll_until_boundary' (e.g. "read every item on
this page") raises mid-run (see adapters/android/factory.py). Direct-action and
single-screen tasks work today.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

# Tags that mark non-GUI-only task subsets; excluded from the default task list (the
# GUI-only subset is the integration target — see the MobileWorld paper).
_NON_GUI_TAGS = ("agent-mcp", "agent-user-interaction")


def _adb_binary() -> str:
    """Resolve the adb binary without importing adbutils."""
    from gui_agent.adapters.android.constants import VENDORED_ADB

    override = os.environ.get("ADBUTILS_ADB_PATH")
    if override:
        return override
    return str(VENDORED_ADB) if VENDORED_ADB.exists() else "adb"


def _run_adb(
    serial: str,
    args: list[str],
    *,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    cmd = [_adb_binary()]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _run_adb_bytes(
    serial: str,
    args: list[str],
    *,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [_adb_binary()]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def _adb_connect(serial: str, *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_adb_binary(), "connect", serial],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _adb_screencap_ready(serial: str) -> tuple[bool, str]:
    try:
        shot = _run_adb_bytes(serial, ["exec-out", "screencap", "-p"], timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        return False, f"screencap failed: {exc}"
    data = shot.stdout or b""
    if shot.returncode != 0:
        detail = (shot.stderr or b"").decode("utf-8", errors="replace").strip()
        return False, f"screencap rc={shot.returncode}: {detail}"
    if len(data) <= 100 or not data.startswith(b"\x89PNG"):
        head = data[:32].decode("utf-8", errors="replace").replace("\n", "\\n")
        return False, f"screencap not png len={len(data)} head={head!r}"
    return True, "screencap ok"


def _wait_for_adb_ready(
    serial: str,
    *,
    timeout_s: float = 120.0,
    interval_s: float = 2.0,
) -> None:
    """Wait for the host-side adb transport to be usable after MobileWorld snapshot load.

    ``/task/init`` loads an emulator snapshot. On this runtime adb commonly reports
    ``offline`` briefly after the HTTP call returns, and taking a screenshot immediately
    can fail the run before the emulator has settled. This is MobileWorld-specific
    because ordinary android sessions do not bounce through backend snapshots.
    """
    deadline = time.monotonic() + max(1.0, timeout_s)
    last = ""
    stable = 0
    while time.monotonic() < deadline:
        if ":" in serial:
            try:
                _adb_connect(serial, timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                last = f"adb connect failed: {exc}"

        try:
            state = _run_adb(serial, ["get-state"], timeout=5.0)
            boot = _run_adb(serial, ["shell", "getprop", "sys.boot_completed"], timeout=5.0)
            size = _run_adb(serial, ["shell", "wm", "size"], timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        else:
            state_text = (state.stdout or state.stderr).strip()
            boot_text = (boot.stdout or boot.stderr).strip()
            size_text = (size.stdout or size.stderr).strip()
            shot_ok = False
            shot_detail = "screencap not checked"
            if (
                state.returncode == 0
                and state_text == "device"
                and boot_text == "1"
                and "Physical size:" in size_text
            ):
                shot_ok, shot_detail = _adb_screencap_ready(serial)
                if shot_ok:
                    stable += 1
                    if stable >= 2:
                        return
                else:
                    stable = 0
                    last = f"state={state_text!r} boot={boot_text!r} size={size_text!r} {shot_detail}"
            else:
                stable = 0
                last = f"state={state_text!r} boot={boot_text!r} size={size_text!r}"
        time.sleep(interval_s)
    raise RuntimeError(f"adb device not ready after {timeout_s:.0f}s ({serial}): {last}")


def _normalize_android_proxy(target: str) -> tuple[str, str]:
    raw = (target or "").strip()
    for prefix in ("http://", "https://"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    raw = raw.split("/", 1)[0]
    host, sep, port = raw.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"invalid MW_ANDROID_HTTP_PROXY={target!r}; expected host:port")
    return host, port


def _configure_android_http_proxy(serial: str, target: str) -> None:
    host, port = _normalize_android_proxy(target)
    proxy = f"{host}:{port}"
    exclusions = os.environ.get(
        "MW_ANDROID_HTTP_PROXY_EXCLUDE",
        "10.0.2.2,localhost,127.0.0.1",
    ).strip()
    commands = (
        ["shell", "settings", "put", "global", "http_proxy", proxy],
        ["shell", "settings", "put", "global", "global_http_proxy_host", host],
        ["shell", "settings", "put", "global", "global_http_proxy_port", port],
        ["shell", "settings", "put", "global", "global_http_proxy_exclusion_list", exclusions],
    )
    for args in commands:
        result = _run_adb(serial, list(args), timeout=10.0)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"failed to configure Android proxy via adb: {detail}")


def _mobileworld_init_retries() -> int:
    raw = (os.environ.get("MW_INIT_RETRIES") or "1").strip()
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise ValueError(f"invalid MW_INIT_RETRIES={raw!r}; expected integer") from exc


def _run_emulator_restart_cmd() -> bool:
    raw = (os.environ.get("MW_EMULATOR_RESTART_CMD") or "").strip()
    if not raw or raw.lower() in {"0", "false", "none", "off"}:
        return False
    timeout_s = float(os.environ.get("MW_EMULATOR_RESTART_TIMEOUT") or "240")
    print(f"[mobileworld] restarting emulator: {raw}")
    subprocess.run(shlex.split(raw), check=True, timeout=timeout_s)
    return True


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

    def reinitialize_controller(self) -> None:
        """Force the backend controller to bind the current emulator process again."""
        self._initialized = False
        self.ensure_init()

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


def _final_answer(result: dict) -> str:
    """Best-effort final answer text for answer-style tasks: prefer collected content
    notes, then the run's result summary. Empty when the run produced neither."""
    notes = result.get("content_notes") or []
    if notes:
        return "\n".join(str(n) for n in notes)
    return str(result.get("result_summary") or "").strip()


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
    parser.add_argument("--no-orchestrator", action="store_true", help="use the legacy milestone DAG path instead of the DSL orchestrator")
    parser.add_argument("--no-dynamic-max-turns", action="store_true", help="do not raise max_turns from DSL program complexity")
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
        print(f"[mobileworld] WARN backend /health not ok at {args.base_url}")
        if _run_emulator_restart_cmd():
            env.reinitialize_controller()
        else:
            print("[mobileworld] WARN no emulator restart command configured; continuing")
    goal = env.get_goal(args.task)
    print(f"[mobileworld] task: {args.task}")
    print(f"[mobileworld] goal: {goal}")
    print(f"[mobileworld] backend: {args.base_url}   adb: {args.adb_serial}")

    intent = goal
    action_policy = build_policy("android_vision")
    supervisor = build_supervisor("milestone")
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
            ready_timeout = float(os.environ.get("MW_ADB_READY_TIMEOUT") or "120")
            max_retries = _mobileworld_init_retries()
            for attempt in range(max_retries + 1):
                try:
                    print(f"[mobileworld] init_task {args.task} (resetting app state)...")
                    env.init_task(args.task)
                    print("[mobileworld] init_task OK")
                    print(f"[mobileworld] waiting for adb after snapshot ({args.adb_serial})...")
                    _wait_for_adb_ready(args.adb_serial, timeout_s=ready_timeout)
                    print("[mobileworld] adb ready")
                    android_proxy = os.environ.get("MW_ANDROID_HTTP_PROXY")
                    if android_proxy and android_proxy.strip().lower() not in {"0", "false", "none", "off"}:
                        proxy_host, proxy_port = _normalize_android_proxy(android_proxy)
                        _configure_android_http_proxy(args.adb_serial, android_proxy)
                        print(f"[mobileworld] android proxy restored: {proxy_host}:{proxy_port}")
                    return
                except Exception as exc:  # noqa: BLE001
                    if attempt >= max_retries:
                        raise
                    print(
                        f"[mobileworld] init/adb readiness failed ({exc}); "
                        f"restart+retry {attempt + 1}/{max_retries}"
                    )
                    if not _run_emulator_restart_cmd():
                        raise
                    env.reinitialize_controller()

        result: dict = {}
        try:
            bundle = build_platform()
            setup = bundle.setup_check()
            if not setup.ok and _run_emulator_restart_cmd():
                env.reinitialize_controller()
                setup = bundle.setup_check()
            for line in setup.lines:
                print(line)
            if not setup.ok:
                result = {
                    "task_type": "RETRIEVE",
                    "goal_completed": False,
                    "stop_reason": f"环境检查未通过：{setup.summary}",
                    "result_summary": f"环境检查未通过：{setup.summary}",
                    "content_notes": None,
                }
            else:
                program = None
                orchestrator_context_reports: list[dict] = []
                run_max_turns = args.max_turns
                _redecompose = None
                with bundle.open_session() as platform:
                    _prime(platform)

                    if not args.no_orchestrator:
                        initial_png = None
                        cur_site = knowledge.app_name if knowledge is not None else ""
                        try:
                            initial_obs = bundle.make_perception(
                                platform, log_dir / "screenshot_initial.png"
                            ).observe()
                            initial_png = initial_obs.png_bytes
                        except Exception as exc:  # noqa: BLE001
                            print(f"[mobileworld] initial observe failed; decompose without screenshot ({exc})")

                        from gui_agent.core.orchestrator import (
                            decompose,
                            estimate_program_turns,
                            normalize_confirm_read_gates,
                            normalize_precondition_gates,
                            redecompose,
                        )
                        from gui_agent.core.supervisor.milestone.helpers import resolve_file_refs
                        from gui_agent.core.router import resolve_intent

                        file_section = resolve_file_refs(intent)
                        resolution = resolve_intent(intent)
                        if resolution.entities:
                            print("[mobileworld] intent: " + "; ".join(
                                f"{e.mention}→{e.type}/{e.match_mode}/key={e.search_key}" for e in resolution.entities))
                        program = normalize_precondition_gates(
                            normalize_confirm_read_gates(
                                decompose(
                                    intent,
                                    knowledge=knowledge.navigation if knowledge else "",
                                    file_section=file_section,
                                    current_url="",
                                    current_title="",
                                    current_site=cur_site,
                                    table_summaries=None,
                                    png_bytes=initial_png,
                                    prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                                    context_reports=orchestrator_context_reports,
                                    resolution=resolution,
                                )
                            )
                        )
                        if file_section and hasattr(supervisor, "_global_constraints"):
                            cap = 3000
                            supervisor._global_constraints.append(
                                file_section if len(file_section) <= cap
                                else file_section[:cap] + "\n…（配置过长已截断，其余以分解结果为准）"
                            )
                        print(f"[mobileworld] orchestrator: {len(program.statements)} statements")

                        def _redecompose(directive: str, context_reports=None, *, observation=None,
                                         prior_experience="", remaining_plan="",
                                         _goal=intent, _know=knowledge, _file=file_section,
                                         _site=cur_site, _png=initial_png, _res=resolution):
                            _cur_png = getattr(observation, "png_bytes", None) or _png
                            return normalize_precondition_gates(normalize_confirm_read_gates(redecompose(
                                _goal, knowledge=_know.navigation if _know else "", file_section=_file,
                                current_url="", current_title="", current_site=_site,
                                table_summaries=None, png_bytes=_cur_png,
                                prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                                corrective_directive=directive, resolution=_res,
                                prior_experience=prior_experience, remaining_plan=remaining_plan,
                                context_reports=context_reports,
                            )))

                        if not args.no_dynamic_max_turns:
                            run_max_turns = estimate_program_turns(program, floor=args.max_turns)
                            if run_max_turns != args.max_turns:
                                print(f"[mobileworld] orchestrator: max_turns {args.max_turns} -> {run_max_turns}")
                    else:
                        print("[mobileworld] orchestrator: disabled; using legacy milestone DAG")

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
                            redecompose=_redecompose,
                            orchestrator_context_reports=orchestrator_context_reports,
                            stop_requested=esc_stop.requested if esc_stop.enabled else None,
                            platform=platform,
                        )

            # ----- post-run: answer bridge + official state-based eval -----
            result = result or {}
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
