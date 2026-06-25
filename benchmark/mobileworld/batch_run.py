"""MobileWorld 批量跑测 —— 顺序跑一组任务,逐条落盘(README 回填 + inline 报告)。

用法:
  source /tmp/mw_proxy.env
  uv run python benchmark/mobileworld/batch_run.py --tasks "$(cat /tmp/nonnet_tasks.json)" ...
或直接传任务名:
  uv run python benchmark/mobileworld/batch_run.py AdjustBrightnessMaximumTask SetAlarmTask

每条任务:前置轻恢复(adb reconnect + /init)→ bin/mobileworld --headless <task>(带超时)
→ 从子进程 stdout 解析 run_dir + OFFICIAL_EVAL(score/reason)→ 回填 README 该行
→ 生成 benchmark/mobileworld/reports/<task>.html。单条崩溃/超时不中断后续。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MW_DIR = Path(__file__).resolve().parent
README = MW_DIR / "README.md"
REPORTS = MW_DIR / "reports"
ADB_S = "192.168.31.57:5556"
BASE = "http://192.168.31.57:6800"
DEVICE = "emulator-5554"
PER_TASK_TIMEOUT = 1800          # 30 min 硬上限(本地任务应远低于此)
PROGRESS = Path("/tmp/mw_batch_progress.log")

_EVAL_RE = re.compile(r"\[mobileworld\] OFFICIAL_EVAL score=([0-9.]+)\s+reason=(.+?)$", re.M)
_RUNDIR_RE = re.compile(r"\[mobileworld\] agent logs:\s*(.+)$", re.M)


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with PROGRESS.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def light_recover() -> None:
    """每条任务前的轻恢复:adb 重连 + 后端 /init。agent 的 _prime 也会自愈,这只是减重试。"""
    for cmd in (["adb", "reconnect", "offline"], ["adb", "connect", ADB_S]):
        subprocess.run(cmd, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        subprocess.run(
            ["curl", "-s", "-m", "15", "-X", "POST", BASE + "/init",
             "-H", "Content-Type: application/json", "-d", json.dumps({"device": DEVICE})],
            timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        pass


def run_task(task: str) -> tuple[str | None, float | None, str]:
    """跑一条任务,返回 (run_dir, score, reason)。score=None 表示没到 eval(崩溃/超时)。"""
    cmd = ["bin/mobileworld", "--headless", task]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              timeout=PER_TASK_TIMEOUT)
        out = proc.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        log(f"  ⏱ {task} 超时({PER_TASK_TIMEOUT}s)")
    m = _RUNDIR_RE.search(out)
    run_dir = m.group(1).strip() if m else None
    em = _EVAL_RE.search(out)
    score = float(em.group(1)) if em else None
    reason = em.group(2).strip().strip("'\"") if em else ""
    return run_dir, score, reason, out


def gen_report(task: str, run_dir: str | None) -> bool:
    if not run_dir:
        return False
    rd = Path(run_dir)
    if not rd.is_dir() or not (rd / "report.html").exists():
        return False
    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / f"{task}.html"
    try:
        subprocess.run(
            ["uv", "run", "python", "-m", "gui_agent.reports.inline", str(rd), "-o", str(out)],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        return out.is_file()
    except Exception:  # noqa: BLE001
        return False


def update_readme(task: str, score: float | None, reason: str, has_report: bool) -> None:
    text = README.read_text(encoding="utf-8")
    lines = text.splitlines()
    changed = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = ln.split("|")
        # cells: ['', ' task ', ' score ', ' status ', ' goal ', ' report ', '']
        if len(cells) < 6 or cells[1].strip() != task:
            continue
        if score is not None and score >= 1.0:
            score_str, status = f"{score:g}", "✅ SUCCESS"
        elif score is not None:
            score_str, status = f"{score:g}", "❌ FAIL"
        else:
            score_str, status = "—", "💥 crashed"
        report = f"[report](reports/{task}.html)" if has_report else "—"
        title = f" title='{reason}'" if reason else ""
        cells[2] = f" {score_str} "
        cells[3] = f" {status}{title} " if title else f" {status} "
        cells[5] = f" {report} "
        lines[i] = "|".join(cells)
        changed = True
        break
    if changed:
        README.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")


def parse_tasks_arg(argv_tasks: list[str]) -> list[str]:
    """argv 里可能是任务名列表,或一个 JSON 文件路径/JSON 串({nonnet:[...]}或[...])。"""
    if not argv_tasks:
        return []
    first = argv_tasks[0]
    if len(argv_tasks) == 1 and (first.endswith(".json") or first.startswith("[") or first.startswith("{")):
        if first.endswith(".json"):
            data = json.loads(Path(first).read_text(encoding="utf-8"))
        else:
            data = json.loads(first)
        if isinstance(data, dict):
            for k in ("nonnet", "tasks", "docreader"):
                if k in data:
                    return data[k]
            return list(data.values())[0] if data else []
        return data
    return argv_tasks


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="MobileWorld 批量跑测")
    p.add_argument("tasks", nargs="*", help="任务名,或 JSON 文件/串({nonnet:[...]} 或 [...])")
    p.add_argument("--skip", nargs="*", default=[], help="跳过的任务名(如已跑完的)")
    p.add_argument("--only", nargs="*", default=[], help="只跑这些(覆盖 tasks)")
    args = p.parse_args()
    tasks = args.only or parse_tasks_arg(args.tasks)
    skip = set(args.skip) | {"OpenFlightModeTask"}   # 已跑
    tasks = [t for t in tasks if t not in skip]
    log(f"==== 批量跑测开始: {len(tasks)} 个任务 ====")
    log("队列: " + ", ".join(tasks))
    summary = []
    for idx, task in enumerate(tasks, 1):
        log(f"---- [{idx}/{len(tasks)}] {task} ----")
        light_recover()
        run_dir, score, reason, _ = run_task(task)
        ok_report = gen_report(task, run_dir)
        update_readme(task, score, reason, ok_report)
        tag = ("✅" if score is not None and score >= 1.0 else
               "❌" if score is not None else "💥")
        log(f"  => {task}: {tag} score={score} reason={reason!r} report={'yes' if ok_report else 'no'}")
        summary.append((task, score, tag, reason))
    log("==== 全部完成 ====")
    n_pass = sum(1 for _, s, _, _ in summary if s is not None and s >= 1.0)
    n_fail = sum(1 for _, s, _, _ in summary if s is not None and s < 1.0)
    n_crash = sum(1 for _, s, _, _ in summary if s is None)
    log(f"汇总: ✅{n_pass} ❌{n_fail} 💥{n_crash} / {len(summary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
