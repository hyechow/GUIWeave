"""Sweep MobileWorld GUI-only tasks through the coding orchestrator (no emulator).

Pulls task names + goals + apps from the live MobileWorld backend, then runs
route → resolve_intent → generate_code for each task. No contract grading —
reports executable / source / diagnostics only.

Run:
  uv run python scripts/mobileworld_orchestrator_sweep.py
  uv run python scripts/mobileworld_orchestrator_sweep.py -j 5
  uv run python scripts/mobileworld_orchestrator_sweep.py --task OpenFlightModeTask
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from gui_agent.adapters.android.mobileworld import (  # noqa: E402
    MobileWorldEnv,
    _android_platform_contract,
    _route_mobileworld_goal,
)
from gui_agent.core.orchestrator import generate_code  # noqa: E402
from gui_agent.core.router import resolve_intent  # noqa: E402
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app  # noqa: E402


def _knowledge_for_apps(apps: list[str], intent: str) -> tuple[str, str]:
    contexts: list[str] = []
    names: list[str] = []
    for app in apps:
        knowledge = load_knowledge_for_app(app, "android")
        if knowledge and knowledge.navigation:
            contexts.append(knowledge.orchestrator_context(intent))
            names.append(app)
    return "\n\n".join(contexts), " + ".join(names)


def _catalog(env: MobileWorldEnv, *, gui_only: bool) -> list[dict[str, Any]]:
    """Fetch task list with apps, then goal per task."""
    env.ensure_init()
    raw = env._req("GET", "/task/list").json()
    from gui_agent.adapters.android.mobileworld import _NON_GUI_TAGS

    tasks: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags") or []
        if gui_only and any(t in tags for t in _NON_GUI_TAGS):
            continue
        name = str(item["name"])
        apps = [str(a) for a in (item.get("apps") or [])]
        goal = str(env.get_goal(name) or "").strip()
        tasks.append({
            "task_name": name,
            "apps": apps,
            "tags": list(tags),
            "goal": goal,
        })
    return tasks


def _run_one(task: dict[str, Any]) -> dict[str, Any]:
    goal = task["goal"]
    apps = list(task.get("apps") or [])
    try:
        routed_goal, route_payload = _route_mobileworld_goal(goal)
        knowledge, bound_apps = _knowledge_for_apps(apps, routed_goal)
        resolution = resolve_intent(routed_goal)
        plan = generate_code(
            routed_goal,
            knowledge=knowledge,
            platform_contract=_android_platform_contract(apps),
            resolution=resolution,
            current_site=bound_apps,
        )
        return {
            "task_name": task["task_name"],
            "apps": apps,
            "tags": task.get("tags") or [],
            "goal": goal,
            "routed_goal": routed_goal,
            "route": route_payload,
            "bound_apps": bound_apps,
            "has_knowledge": bool(knowledge.strip()),
            "ok": bool(plan.executable),
            "executable": bool(plan.executable),
            "repaired": bool(plan.repaired),
            "resolution": resolution.model_dump(mode="json"),
            "source": plan.source,
            "attempts": [
                {
                    "diagnostics": [item.render() for item in attempt.diagnostics],
                    "run_ok": attempt.run.ok if attempt.run is not None else None,
                    "run_error": attempt.run.error if attempt.run is not None else "",
                    "source_len": len(attempt.source or ""),
                }
                for attempt in plan.attempts
            ],
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 — isolate worker crashes
        return {
            "task_name": task["task_name"],
            "apps": apps,
            "tags": task.get("tags") or [],
            "goal": goal,
            "routed_goal": "",
            "route": {},
            "bound_apps": "",
            "has_knowledge": False,
            "ok": False,
            "executable": False,
            "repaired": False,
            "resolution": {},
            "source": "",
            "attempts": [],
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=8),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=__import__("os").environ.get("MW_BASE_URL", "http://192.168.1.101:6800"),
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=5,
        help="max concurrent workers (default 5, capped at 5)",
    )
    parser.add_argument("--task", action="append", default=[], help="restrict to task name(s)")
    parser.add_argument("--all-tasks", action="store_true", help="include non-GUI tags")
    parser.add_argument("--list", action="store_true", help="list tasks and exit")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="only first N tasks after filter (0 = all)",
    )
    args = parser.parse_args()
    jobs = max(1, min(5, args.jobs))

    env = MobileWorldEnv(args.base_url)
    if not env.health():
        print(f"[sweep] backend /health not ok at {args.base_url}", file=sys.stderr)
        return 2

    print(f"[sweep] fetching catalog from {args.base_url} ...", flush=True)
    tasks = _catalog(env, gui_only=not args.all_tasks)
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t["task_name"] in wanted]
        missing = wanted - {t["task_name"] for t in tasks}
        if missing:
            print(f"[sweep] missing tasks: {sorted(missing)}", file=sys.stderr)
            return 2
    if args.limit and args.limit > 0:
        tasks = tasks[: args.limit]

    if args.list:
        for t in tasks:
            apps = ",".join(t["apps"]) or "-"
            print(f"{t['task_name']:45s} apps={apps}")
            if t["goal"]:
                print(f"  {t['goal'][:160]}")
        print(f"\n{len(tasks)} tasks")
        return 0

    if not tasks:
        print("[sweep] no tasks selected", file=sys.stderr)
        return 2

    output_dir = (
        ROOT / "logs/orchestrator_eval/android_mw_sweep" / time.strftime("%Y%m%d_%H%M%S")
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = output_dir / "sources"
    sources_dir.mkdir(exist_ok=True)

    results: list[dict[str, Any] | None] = [None] * len(tasks)
    print_lock = threading.Lock()
    write_lock = threading.Lock()
    t0 = time.monotonic()

    def summary() -> dict[str, Any]:
        finished = [r for r in results if r is not None]
        return {
            "base_url": args.base_url,
            "jobs": jobs,
            "total": len(tasks),
            "finished": len(finished),
            "executable_ok": sum(1 for r in finished if r.get("executable")),
            "failed": sum(1 for r in finished if not r.get("ok")),
            "with_knowledge": sum(1 for r in finished if r.get("has_knowledge")),
            "elapsed_s": round(time.monotonic() - t0, 1),
        }

    def emit(index: int, result: dict[str, Any]) -> None:
        with print_lock:
            mark = "PASS" if result.get("ok") else "FAIL"
            apps = ",".join(result.get("apps") or []) or "-"
            extra = ""
            if result.get("error"):
                extra = f" err={result['error'][:120]}"
            elif not result.get("executable"):
                diags = []
                for attempt in result.get("attempts") or []:
                    diags.extend(attempt.get("diagnostics") or [])
                if diags:
                    extra = " | " + "; ".join(diags[:3])
            print(
                f"[{mark}] {index + 1}/{len(tasks)} {result['task_name']} "
                f"apps={apps} knowledge={int(bool(result.get('has_knowledge')))}"
                f"{extra}",
                flush=True,
            )
            src = result.get("source") or ""
            if src:
                preview = src.strip().splitlines()
                for line in preview[:6]:
                    print(f"       {line}", flush=True)
                if len(preview) > 6:
                    print(f"       ... ({len(preview)} lines)", flush=True)

        # Persist per-task source early.
        (sources_dir / f"{result['task_name']}.py").write_text(
            result.get("source") or f"# ERROR: {result.get('error') or 'empty'}\n",
            encoding="utf-8",
        )
        with write_lock:
            results[index] = result
            payload = {
                "summary": summary(),
                "cases": [r for r in results if r is not None],
            }
            (output_dir / "report.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    print(f"[sweep] {len(tasks)} tasks, jobs={jobs}", flush=True)
    print(f"[sweep] output -> {output_dir}", flush=True)

    if jobs == 1 or len(tasks) <= 1:
        for index, task in enumerate(tasks):
            emit(index, _run_one(task))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_run_one, task): index
                for index, task in enumerate(tasks)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "task_name": tasks[index]["task_name"],
                        "apps": tasks[index].get("apps") or [],
                        "tags": tasks[index].get("tags") or [],
                        "goal": tasks[index].get("goal") or "",
                        "ok": False,
                        "executable": False,
                        "error": f"FUTURE:{type(exc).__name__}: {exc}",
                        "source": "",
                        "attempts": [],
                    }
                emit(index, result)

    s = summary()
    print(
        f"\n{s['executable_ok']}/{s['total']} executable  "
        f"failed={s['failed']}  knowledge={s['with_knowledge']}/{s['finished']}  "
        f"elapsed={s['elapsed_s']}s  jobs={jobs}",
        flush=True,
    )
    print(f"report -> {output_dir / 'report.json'}", flush=True)
    # Write a compact markdown summary for quick scan.
    finished = [r for r in results if r is not None]
    lines = [
        f"# MobileWorld orchestrator sweep",
        f"",
        f"- base_url: `{args.base_url}`",
        f"- executable: **{s['executable_ok']}/{s['total']}**",
        f"- with knowledge: {s['with_knowledge']}/{s['finished']}",
        f"- elapsed: {s['elapsed_s']}s  jobs={jobs}",
        f"",
        f"| # | task | apps | exec | knowledge | note |",
        f"|---|------|------|------|-----------|------|",
    ]
    for i, r in enumerate(finished):
        note = r.get("error") or ""
        if not note and not r.get("executable"):
            diags = []
            for attempt in r.get("attempts") or []:
                diags.extend(attempt.get("diagnostics") or [])
            note = "; ".join(diags[:2])
        note = note.replace("|", "/").replace("\n", " ")[:120]
        lines.append(
            f"| {i+1} | `{r['task_name']}` | {','.join(r.get('apps') or []) or '-'} | "
            f"{'Y' if r.get('executable') else 'N'} | "
            f"{'Y' if r.get('has_knowledge') else 'N'} | {note} |"
        )
    (output_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary -> {output_dir / 'SUMMARY.md'}", flush=True)
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
