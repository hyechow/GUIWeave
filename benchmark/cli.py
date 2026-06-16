#!/usr/bin/env python3
"""Benchmark CLI — run benchmark tasks and generate reports."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

HERE = Path(__file__).parent
TASKS_FILE = HERE / "tasks.json"
RESULTS_FILE = HERE / "results.json"
ROOT = HERE.parent

console = Console()


# ── Data helpers ────────────────────────────────────────────────────────────


def _load_tasks() -> list[dict]:
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))


def _save_tasks(tasks: list[dict]) -> None:
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_results() -> dict:
    if RESULTS_FILE.exists():
        return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    return {}


def _save_results(results: dict) -> None:
    RESULTS_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _task_by_id(tasks: list[dict], task_id: str) -> dict | None:
    return next((t for t in tasks if t["id"] == task_id), None)


# ── Commands ────────────────────────────────────────────────────────────────


def cmd_list(args) -> None:
    """List all tasks."""
    tasks = _load_tasks()

    filters = {}
    if args.category:
        filters["category"] = args.category
    if args.app:
        filters["app"] = args.app
    if args.difficulty:
        filters["difficulty"] = args.difficulty

    results = _load_results()

    for task in tasks:
        if filters.get("category") and task["category"] != filters["category"]:
            continue
        if filters.get("difficulty") and task["difficulty"] != filters["difficulty"]:
            continue
        if filters.get("app"):
            apps = task.get("app", [])
            if isinstance(apps, str):
                apps = [apps]
            if filters["app"] not in apps:
                continue

        tid = task["id"]
        res = results.get(tid, {})
        status = res.get("status", "")

        cat = "操作" if task["category"] == "operation" else "查询"
        apps = task.get("app", [])
        if isinstance(apps, list):
            app_str = "→".join(apps)
        else:
            app_str = apps

        diff_color = {"easy": "green", "medium": "yellow", "hard": "red"}.get(
            task["difficulty"], "white"
        )
        status_color = {
            "pass": "green",
            "fail": "red",
            "skip": "dim",
            "running": "cyan",
        }.get(status, "dim")
        status_display = status or "—"

        console.print(
            f"  [{diff_color}]{task['difficulty']:6}[/] "
            f"[{status_color}]{status_display:6}[/] "
            f"[cyan]{tid:11}[/] "
            f"[dim]{cat}[/dim] "
            f"{app_str:10} "
            f"{task['task']}"
        )

    console.print()


def cmd_run(args) -> None:
    """Run one or more tasks."""
    from gui_agent.core.run.io import create_run_dir
    from gui_agent.core.runner import (
        build_policy,
        build_supervisor,
        run_agent_loop,
    )
    from gui_agent.core.policies import StructuredOutputPolicy
    from gui_agent.core.self_learning.app_summary import auto_discover_knowledge

    tasks = _load_tasks()
    results = _load_results()

    task_ids = args.task_ids
    if not task_ids:
        console.print("[red]请指定要运行的任务 ID，或用 --all 运行全部[/red]")
        return

    if "--all" in task_ids:
        task_ids = [t["id"] for t in tasks]

    selected = []
    for tid in task_ids:
        task = _task_by_id(tasks, tid)
        if not task:
            console.print(f"[red]未找到任务: {tid}[/red]")
            continue
        if args.category and task["category"] != args.category:
            continue
        if args.difficulty and task["difficulty"] != args.difficulty:
            continue
        selected.append(task)

    if not selected:
        console.print("[yellow]没有匹配的任务[/yellow]")
        return

    console.print(f"\n[bold]准备运行 {len(selected)} 个任务[/bold]\n")

    for i, task in enumerate(selected, 1):
        tid = task["id"]
        console.print(f"[bold cyan]{'─' * 60}[/bold cyan]")
        console.print(
            f"[bold][{i}/{len(selected)}] {tid}[/bold]  "
            f"[dim]{task['difficulty']}[/dim]  {task['task']}"
        )
        console.print(f"[bold cyan]{'─' * 60}[/bold cyan]\n")

        results[tid] = {
            "task": task["task"],
            "status": "running",
            "started_at": datetime.now().isoformat(),
        }
        _save_results(results)

        try:
            action_policy = build_policy(StructuredOutputPolicy.name)
            supervisor = build_supervisor(args.supervisor)

            knowledge = auto_discover_knowledge(task["task"])
            if knowledge and hasattr(supervisor, "set_app_knowledge"):
                supervisor.set_app_knowledge(
                    knowledge.navigation,
                    app_name=knowledge.app_name,
                    elements=knowledge.elements,
                )

            log_dir = create_run_dir("benchmark")
            context_path = log_dir / "context.json"

            t0 = time.time()
            result = run_agent_loop(
                task["task"],
                action_policy,
                supervisor,
                input_context_path=None,
                log_dir=log_dir,
                context_path=context_path,
                max_turns=args.max_turns,
                auto_continue=True,
            )
            elapsed = time.time() - t0

            ok = result.get("goal_completed", False)
            results[tid] = {
                "task": task["task"],
                "status": "pass" if ok else "fail",
                "goal_completed": ok,
                "stop_reason": result.get("stop_reason", ""),
                "result_summary": result.get("result_summary", ""),
                "turns_count": result.get("turns_count", 0),
                "elapsed": round(elapsed, 1),
                "log_dir": str(log_dir.relative_to(ROOT)),
                "report": str((log_dir / "report.html").relative_to(ROOT)),
                "completed_at": datetime.now().isoformat(),
            }

            # Auto-generate HTML report
            try:
                from scripts.report_builder import RunnerReportBuilder, save_report
                report_data = RunnerReportBuilder().build(log_dir)
                report_path = save_report(report_data, log_dir / "report.html")
                console.print(f"  [dim]Report: {report_path}[/dim]")
            except Exception:
                pass

            icon = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
            console.print(f"\n  {icon}  {result.get('stop_reason', '')}  [dim]{result.get('turns_count', 0)} turns, {elapsed:.1f}s[/dim]\n")

        except Exception as exc:
            results[tid] = {
                "task": task["task"],
                "status": "fail",
                "error": str(exc),
                "completed_at": datetime.now().isoformat(),
            }
            console.print(f"\n  [red]ERROR[/red]  {exc}\n")

        _save_results(results)

    _print_summary(selected, results)


def cmd_rerun(args) -> None:
    """Re-run failed tasks."""
    args.task_ids = []
    results = _load_results()
    failed_ids = [tid for tid, r in results.items() if r.get("status") == "fail"]
    if not failed_ids:
        console.print("[green]没有失败的任务[/green]")
        return
    args.task_ids = failed_ids
    cmd_run(args)


def cmd_result(args) -> None:
    """Show result for a task."""
    results = _load_results()
    tid = args.task_id
    res = results.get(tid)
    if not res:
        console.print(f"[yellow]暂无结果: {tid}[/yellow]")
        return

    status = res.get("status", "—")
    color = {"pass": "green", "fail": "red", "running": "cyan"}.get(status, "dim")

    console.print(f"\n  [bold]{tid}[/bold]  [{color}]{status}[/{color}]")
    for key, val in res.items():
        if key in ("task", "status"):
            continue
        console.print(f"  [dim]{key}:[/dim] {val}")
    console.print()


def cmd_record(args) -> None:
    """Manually record a result for a task."""
    tasks = _load_tasks()
    results = _load_results()

    task = _task_by_id(tasks, args.task_id)
    if not task:
        console.print(f"[red]未找到任务: {args.task_id}[/red]")
        return

    results[args.task_id] = {
        "task": task["task"],
        "status": args.status,
        "notes": args.notes or "",
        "completed_at": datetime.now().isoformat(),
    }
    _save_results(results)
    console.print(f"[green]已记录[/green] {args.task_id} → {args.status}")


def cmd_stats(args) -> None:
    """Show benchmark statistics."""
    tasks = _load_tasks()
    results = _load_results()

    table = Table(title="Benchmark Statistics", show_header=True, header_style="bold")
    table.add_column("Category", style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("Pass", justify="right", style="green")
    table.add_column("Fail", justify="right", style="red")
    table.add_column("Skip", justify="right")
    table.add_column("Remaining", justify="right", style="yellow")
    table.add_column("Rate", justify="right", style="bold")

    for cat_name, cat_key in [("操作类", "operation"), ("查询类", "query"), ("合计", None)]:
        if cat_key:
            cat_tasks = [t for t in tasks if t["category"] == cat_key]
        else:
            cat_tasks = tasks

        total = len(cat_tasks)
        pass_count = sum(1 for t in cat_tasks if results.get(t["id"], {}).get("status") == "pass")
        fail_count = sum(1 for t in cat_tasks if results.get(t["id"], {}).get("status") == "fail")
        skip_count = sum(1 for t in cat_tasks if results.get(t["id"], {}).get("status") == "skip")
        remaining = total - pass_count - fail_count - skip_count
        rate = f"{pass_count / total:.0%}" if total and (pass_count + fail_count) else "—"

        table.add_row(cat_name, str(total), str(pass_count), str(fail_count), str(skip_count), str(remaining), rate)

    console.print(table)

    # Per-app breakdown
    app_stats: dict[str, dict] = {}
    for t in tasks:
        apps = t.get("app", [])
        if isinstance(apps, str):
            apps = [apps]
        for app in apps:
            if app not in app_stats:
                app_stats[app] = {"total": 0, "pass": 0, "fail": 0}
            app_stats[app]["total"] += 1
            status = results.get(t["id"], {}).get("status", "")
            if status == "pass":
                app_stats[app]["pass"] += 1
            elif status == "fail":
                app_stats[app]["fail"] += 1

    if app_stats:
        console.print()
        app_table = Table(title="Per-App Breakdown", show_header=True, header_style="bold")
        app_table.add_column("App", style="cyan")
        app_table.add_column("Total", justify="right")
        app_table.add_column("Pass", justify="right", style="green")
        app_table.add_column("Fail", justify="right", style="red")
        app_table.add_column("Rate", justify="right")

        for app in sorted(app_stats):
            s = app_stats[app]
            rate = f"{s['pass'] / (s['pass'] + s['fail']):.0%}" if (s["pass"] + s["fail"]) else "—"
            app_table.add_row(app, str(s["total"]), str(s["pass"]), str(s["fail"]), rate)

        console.print(app_table)
    console.print()


def _render_benchmark_tables(tasks: list[dict], results: dict) -> str:
    operations = [t for t in tasks if t["category"] == "operation"]
    queries = [t for t in tasks if t["category"] == "query"]

    def app_label(t):
        apps = t.get("app", [])
        return "→".join(apps) if isinstance(apps, list) else apps

    def render_table(items, title):
        lines = [
            f"### {title}\n",
            "| ID | 任务 | App | 难度 | 状态 | 结果 | 备注 |",
            "|---|---|---|---|---|---|---|",
        ]
        for t in items:
            tid = t["id"]
            task = t["task"]
            app = app_label(t)
            diff = t["difficulty"]
            res = results.get(tid, {})
            status = res.get("status", "")
            summary = res.get("result_summary", "") or res.get("error", "")
            notes = res.get("notes", "")
            lines.append(f"| {tid} | {task} | {app} | {diff} | {status} | {summary} | {notes} |")
        return "\n".join(lines)

    md = render_table(operations, f"操作类（{len(operations)}）")
    md += "\n\n"
    md += render_table(queries, f"查询类（{len(queries)}）")
    md += "\n"
    return md


def cmd_generate(_args) -> None:
    """Inject benchmark tables into README.md."""
    tasks = _load_tasks()
    results = _load_results()
    readme_path = HERE / "README.md"
    readme = readme_path.read_text(encoding="utf-8")

    marker_start = "<!-- BENCHMARK_START -->"
    marker_end = "<!-- BENCHMARK_END -->"

    start_idx = readme.find(marker_start)
    end_idx = readme.find(marker_end)

    if start_idx == -1 or end_idx == -1:
        console.print("[red]README.md 中未找到 BENCHMARK 标记[/red]")
        return

    table_md = _render_benchmark_tables(tasks, results)
    new_readme = readme[: start_idx + len(marker_start)] + "\n" + table_md + "\n" + readme[end_idx:]
    readme_path.write_text(new_readme, encoding="utf-8")

    console.print(f"[green]Generated README.md[/green] ({len(tasks)} tasks)")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _print_summary(tasks: list[dict], results: dict) -> None:
    total = len(tasks)
    passed = sum(1 for t in tasks if results.get(t["id"], {}).get("status") == "pass")
    failed = sum(1 for t in tasks if results.get(t["id"], {}).get("status") == "fail")
    console.print(f"\n[bold]结果汇总: {passed}/{total} passed, {failed} failed[/bold]")

    for t in tasks:
        r = results.get(t["id"], {})
        report = r.get("report")
        if report:
            console.print(f"  [link=file://{report}]{t['id']} report: {report}[/link]")

    console.print()


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark CLI — 运行测试任务并生成报告",
        prog="benchmark_cli",
    )
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="列出所有任务")
    p_list.add_argument("--category", "-c", choices=["operation", "query"], help="按类别筛选")
    p_list.add_argument("--app", "-a", help="按 App 筛选")
    p_list.add_argument("--difficulty", "-d", choices=["easy", "medium", "hard"], help="按难度筛选")

    # run
    p_run = sub.add_parser("run", help="运行指定任务")
    p_run.add_argument("task_ids", nargs="*", help="任务 ID，或 --all")
    p_run.add_argument("--all", action="store_true", help="运行全部任务")
    p_run.add_argument("--category", "-c", choices=["operation", "query"])
    p_run.add_argument("--difficulty", "-d", choices=["easy", "medium", "hard"])
    p_run.add_argument("--supervisor", choices=["simple", "milestone"], default="milestone")
    p_run.add_argument("--max-turns", type=int, default=20)

    # rerun
    p_rerun = sub.add_parser("rerun", help="重跑失败的任务")
    p_rerun.add_argument("--supervisor", choices=["simple", "milestone"], default="milestone")
    p_rerun.add_argument("--max-turns", type=int, default=20)

    # result
    p_result = sub.add_parser("result", help="查看任务结果")
    p_result.add_argument("task_id")

    # record
    p_record = sub.add_parser("record", help="手动记录任务结果")
    p_record.add_argument("task_id")
    p_record.add_argument("status", choices=["pass", "fail", "skip"])
    p_record.add_argument("--notes", "-n", default="")

    # stats
    sub.add_parser("stats", help="查看统计信息")

    # generate
    sub.add_parser("generate", help="生成 benchmark.md")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Route --all flag for run command
    if args.command == "run" and getattr(args, "all", False):
        args.task_ids = [t["id"] for t in _load_tasks()]

    commands = {
        "list": cmd_list,
        "run": cmd_run,
        "rerun": cmd_rerun,
        "result": cmd_result,
        "record": cmd_record,
        "stats": cmd_stats,
        "generate": cmd_generate,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)


if __name__ == "__main__":
    main()
