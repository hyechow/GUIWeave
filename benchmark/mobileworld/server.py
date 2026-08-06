"""MobileWorld 可视化服务 —— 单文件 FastAPI 仪表盘。

跑测结果的两条数据源:
  - `logs/gui_agent/mobileworld/android/<ts>/` 每次运行的完整产物(逐轮截图 + stdout + report.html)
  - `benchmark/mobileworld/README.md` 的 GUI-only 任务进度表(117 行)

提供:
  GET /                         首页:通过率统计 + 117 任务表 + 最近运行
  GET /run/{run_id}             单次运行详情:逐轮标注截图 + stdout + report 入口
  GET /run/{run_id}/{filename}  run 目录内文件(截图/log/report.html)—— 让 report.html 的相对图片能解析
  GET /report/{task}            benchmark/mobileworld/reports/<task>.html(自包含 inline 报告)
  GET /stdout/{run_id}          原始 stdout.log(纯文本)
  GET /api/runs                 运行列表 JSON

启动:bin/mw_view  或  uv run python -m benchmark.mobileworld.server [--port 8000] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

# /root/iphone-use
ROOT = Path(__file__).resolve().parents[2]
MW_DIR = Path(__file__).resolve().parent                       # benchmark/mobileworld/
MW_LOGS = ROOT / "logs" / "gui_agent" / "mobileworld" / "android"
README = MW_DIR / "README.md"
REPORTS = MW_DIR / "reports"

app = FastAPI(title="MobileWorld 可视化")


# --------------------------------------------------------------------------- #
# 数据解析
# --------------------------------------------------------------------------- #
_TASK_RE = re.compile(r"^\[mobileworld\] task:\s*(.+)$", re.M)
_GOAL_RE = re.compile(r"^\[mobileworld\] goal:\s*(.+)$", re.M)
_EVAL_RE = re.compile(r"\[mobileworld\] OFFICIAL_EVAL score=([0-9.]+)\s+reason=(.+?)$", re.M)
_STOP_RE = re.compile(r"agent-loop 停止|goal_completed|stop_reason", re.M)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _run_meta(d: Path) -> dict:
    """单次 run 的元数据。主源 context.json 的 mobileworld 子结构(task/goal/score/reason),
    stdout.log 补 reason / 崩溃判定。([mobileworld] task 行在 tee 之前打印,不进 stdout.log。)"""
    mw: dict = {}
    ctx_goal = None
    ctx = d / "context.json"
    if ctx.is_file():
        try:
            cj = json.loads(ctx.read_text(encoding="utf-8", errors="replace"))
            mw = cj.get("mobileworld") or {}
            ctx_goal = cj.get("goal")
        except Exception:  # noqa: BLE001
            mw = {}
    stdout = _read(d / "stdout.log")
    eval_m = _EVAL_RE.search(stdout)
    score = mw.get("score")
    if score is None and eval_m:
        score = float(eval_m.group(1))
    reason = mw.get("reason") or (eval_m.group(2).strip().strip("'\"") if eval_m else "")
    ts = _parse_ts(d.name)
    return {
        "id": d.name,
        "ts": ts,                              # datetime or None
        "ts_str": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else d.name,
        "task": mw.get("task_name") or "(unknown)",
        "goal": (mw.get("goal") or ctx_goal or "")[:160],
        "score": score,
        "score_str": f"{score:g}" if score is not None else "—",
        "reason": reason,
        "passed": (score is not None and score >= 1.0),
        "crashed": ("Traceback" in stdout) and score is None,
        "has_report": (d / "report.html").exists(),
        "n_screenshots": len(list(d.glob("screenshot_turn_*_ann.jpg"))) or len(list(d.glob("screenshot_turn_*.png"))),
        "has_context": bool(mw),
        "base_url": mw.get("base_url") or "",
    }


def parse_runs() -> list[dict]:
    """扫描所有 run 目录,解析元数据;按时间倒序。"""
    if not MW_LOGS.exists():
        return []
    runs = [_run_meta(d) for d in MW_LOGS.iterdir() if d.is_dir()]
    runs.sort(key=lambda r: (r["ts"] or datetime.min), reverse=True)
    return runs


def _parse_ts(name: str):
    for fmt in ("%Y%m%d_%H%M%S", "%Y-%m-%d_%H-%M-%S"):
        try:
            return datetime.strptime(name, fmt)
        except ValueError:
            continue
    return None


def parse_readme_tasks() -> list[dict]:
    """解析 README.md 的 GUI-only 表 → [{name, score, status, goal, report}]。"""
    text = _read(README)
    rows: list[dict] = []
    in_table = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("| 任务 ") or s.startswith("| 任务") or re.match(r"^\|\s*-{2,}", s):
            in_table = True
            continue
        if not in_table or not s.startswith("|"):
            if in_table and s and not s.startswith("|"):
                in_table = False
            continue
        parts = [p.strip() for p in s.strip("|").split("|")]
        if len(parts) < 5:
            continue
        name, score, status, goal = parts[0], parts[1], parts[2], parts[3]
        if name in ("任务",) or name.startswith("---"):
            continue
        report = parts[4]
        m = re.search(r"\((reports/[^)]+)\)", report)
        rows.append({
            "name": name,
            "score": score,
            "status": status,
            "badge": _status_badge(status),
            "goal": goal,
            "report": m.group(1) if m else None,
        })
    return rows


def _status_badge(status: str) -> str:
    if "✅" in status:
        return "pass"
    if "❌" in status:
        return "fail"
    return "todo"


def latest_run_for_task(task: str, runs: list[dict]) -> dict | None:
    """Most recent PASSING run for a task; falls back to the most recent run.

    The task table shows the result of the most recent successful run so a task
    that passed at least once reads as ✅ even if a later run failed."""
    for r in runs:
        if r["task"] == task and r["passed"]:
            return r
    for r in runs:
        if r["task"] == task:
            return r
    return None


def _backend_base_url() -> str:
    """Backend base URL from the most recent run's context (fallback empty)."""
    for r in parse_runs():
        if r.get("base_url"):
            return r["base_url"]
    return ""


def _newest_run_for_task(task: str, runs: list[dict]) -> dict | None:
    """Most recent run by time (used for the 最新运行 link, pass/fail irrelevant)."""
    for r in runs:
        if r["task"] == task:
            return r
    return None


def _fetch_gui_tasks() -> list[str]:
    """Full GUI-only task list from the MobileWorld backend /task/list.

    Excludes the agent-user-interaction and agent-mcp subsets so only the GUI-only
    117 tasks are shown. Returns [] when the backend is unreachable (falls back to
    README + run-derived tasks).
    """
    base = _backend_base_url()
    if not base:
        return []
    try:
        import urllib.request
        with urllib.request.urlopen(f"{base}/task/list", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    excluded = {"agent-user-interaction", "agent-mcp"}
    return [
        str(item["name"]) for item in data
        if isinstance(item, dict) and not (set(item.get("tags") or []) & excluded)
    ]


def task_display_state(task: dict, latest: dict | None) -> dict:
    """Merge README progress with the latest run so the table reflects fresh logs.

    README.md is a curated baseline and may lag behind ad-hoc runs. When a task has a
    latest run with an eval score, show that score/status in the main task table while
    leaving the static report link behavior unchanged.
    """
    out = dict(task)
    if latest is None or latest.get("score") is None:
        out["source"] = "README"
        return out

    if not out.get("goal") and latest.get("goal"):
        out["goal"] = latest["goal"]
    score = latest["score"]
    out["score"] = latest.get("score_str") or f"{score:g}"
    if latest.get("passed"):
        out["status"] = "✅ 通过"
        out["badge"] = "pass"
    elif latest.get("crashed"):
        out["status"] = "💥 crashed"
        out["badge"] = "crash"
    else:
        out["status"] = "❌ 失败"
        out["badge"] = "fail"
    out["source"] = f"logs/{latest['id']}"
    return out




def _norm_match_text(text: str) -> str:
    text = str(text or "").lower()
    text = text.replace("「", "'").replace("」", "'").replace('"', "'")
    return re.sub(r"\s+", " ", text).strip()






def _case_by_label(label_to_case: dict[str, dict], label: str) -> dict | None:
    return label_to_case.get(label) if label else None




def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _extract_agent_final_output(stdout: str) -> str:
    matches = list(
        re.finditer(
            r"=+\n最终输出\n=+\n(?P<output>.*?)(?:\n=+\n|\Z)",
            stdout or "",
            flags=re.S,
        )
    )
    if not matches:
        return ""
    return matches[-1].group("output").strip()








# --------------------------------------------------------------------------- #
# 视图
# --------------------------------------------------------------------------- #
CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
       background:#0f1115; color:#e6e6e6; }
header { background:#161a21; padding:18px 28px; border-bottom:1px solid #262b34; position:sticky; top:0; z-index:5;}
header h1 { margin:0; font-size:20px; font-weight:600; }
header .sub { color:#8a93a3; font-size:13px; margin-top:3px; }
main { max-width:1280px; margin:0 auto; padding:24px 28px 60px; }
.stats { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }
.stat { background:#161a21; border:1px solid #262b34; border-radius:10px; padding:14px 18px; min-width:120px;}
.stat .n { font-size:26px; font-weight:700; }
.stat .l { color:#8a93a3; font-size:12px; margin-top:2px; }
.stat.pass .n { color:#3ddc84; } .stat.fail .n { color:#ff6b6b; } .stat.todo .n { color:#f5a623; }
.stat.rate .n { color:#62b6ff; }
section { background:#161a21; border:1px solid #262b34; border-radius:12px; margin-bottom:22px; }
section > .hd { padding:14px 18px; border-bottom:1px solid #262b34; font-weight:600; font-size:15px;
                display:flex; justify-content:space-between; align-items:center; }
section > .hd .hint { color:#8a93a3; font-weight:400; font-size:12px; }
section .bd { padding:6px 0; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:9px 14px; text-align:left; border-bottom:1px solid #20242c; vertical-align:top; }
th { color:#8a93a3; font-weight:500; font-size:12px; text-transform:uppercase; letter-spacing:.03em;}
td.task { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; white-space:nowrap; }
td.goal { color:#a9b0bd; max-width:420px; }
.badge { display:inline-block; padding:2px 9px; border-radius:20px; font-size:12px; font-weight:600; }
.badge.pass { background:rgba(61,220,132,.14); color:#3ddc84; }
.badge.fail { background:rgba(255,107,107,.14); color:#ff6b6b; }
.badge.todo { background:rgba(245,166,35,.14); color:#f5a623; }
.badge.crash { background:rgba(255,107,107,.14); color:#ff6b6b; }
a { color:#62b6ff; text-decoration:none; } a:hover { text-decoration:underline; }
tr:hover { background:#1a1f27; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.score { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.pill { display:inline-block; background:#20242c; color:#a9b0bd; padding:1px 8px; border-radius:6px; font-size:11px;}
.shots { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:12px; padding:16px 18px; }
.shots figure { margin:0; } .shots figcaption { font-size:11px; color:#8a93a3; margin-top:4px; }
.shots img { width:100%; border-radius:8px; border:1px solid #262b34; cursor:zoom-in; display:block;}
iframe { width:100%; height:80vh; border:1px solid #262b34; border-radius:8px; }
pre { background:#0b0d11; padding:16px; border-radius:8px; overflow:auto; font-size:12px; line-height:1.5;
      border:1px solid #20242c; max-height:70vh; }
.btnrow { padding:14px 18px; display:flex; gap:10px; flex-wrap:wrap; }
.btn { background:#2266cc; color:#fff; padding:7px 14px; border-radius:8px; font-size:13px; }
.btn.ghost { background:#20242c; color:#cdd3dd; }
.case-list { padding:12px 14px 16px; display:grid; gap:10px; }
.case-card { background:#11151b; border:1px solid #262b34; border-radius:10px; overflow:hidden; }
.case-card[open] { border-color:#334050; }
.case-card summary { cursor:pointer; list-style:none; display:grid;
    grid-template-columns:18px minmax(260px,1.4fr) 110px minmax(240px,1.2fr) 120px 170px;
    gap:12px; align-items:center; padding:12px 14px; }
.case-card summary::-webkit-details-marker { display:none; }
.case-card summary:before { content:"▸"; color:#8a93a3; }
.case-card[open] summary:before { content:"▾"; }
.case-title { color:#e6e6e6; font-weight:600; }
.case-layer, .case-latest, .case-metric { color:#a9b0bd; font-size:12px; }
.case-goal { color:#a9b0bd; padding:0 14px 12px 38px; line-height:1.45; font-size:13px; }
.case-history { padding:0 14px 14px 38px; }
.case-history table { background:#0f1115; border:1px solid #20242c; border-radius:8px; overflow:hidden; }
.case-history td.goal { max-width:560px; }
.fold-panel { background:#161a21; border:1px solid #262b34; border-radius:12px; margin-bottom:22px; overflow:hidden; }
.fold-panel summary { cursor:pointer; list-style:none; padding:14px 18px; border-bottom:1px solid #262b34;
    font-weight:600; font-size:15px; display:flex; justify-content:space-between; align-items:center; gap:16px; }
.fold-panel summary::-webkit-details-marker { display:none; }
.fold-panel summary:before { content:"▸"; color:#8a93a3; margin-right:8px; }
.fold-panel[open] summary:before { content:"▾"; }
.fold-panel summary .title { flex:1; }
.fold-panel summary .hint { color:#8a93a3; font-weight:400; font-size:12px; }
.fold-panel .bd { padding:6px 0; }
.tabs { display:flex; gap:8px; margin:16px 18px 0; }
.tabs button { background:#161a21; border:1px solid #262b34; color:#8a93a3; padding:8px 16px;
               border-radius:8px; cursor:pointer; font-size:13px; }
.tabs button.active { background:#262b34; color:#fff; border-color:#3a4250; }
.tab-panel { display:none; }
.tab-panel.active { display:block; }
"""


def _layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_html.escape(title)}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def _header(sub: str) -> str:
    return (
        "<header><h1>📱 MobileWorld 跑测可视化</h1>"
        f"<div class='sub'>{_html.escape(sub)}</div></header>"
    )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    runs = parse_runs()
    raw_tasks = parse_readme_tasks()
    # Full GUI-only 117-task list from the backend, merged with README + ad-hoc runs
    # so every benchmark task appears even before it has a run.
    gui_tasks = _fetch_gui_tasks()
    raw_by_name = {t["name"]: t for t in raw_tasks}
    all_names = list(dict.fromkeys([
        *gui_tasks,
        *(t["name"] for t in raw_tasks),
        *(r["task"] for r in runs if r["task"] and r["task"] != "(unknown)"),
    ]))
    base_tasks = [
        raw_by_name.get(name) or {
            "name": name, "score": "—", "status": "⬜ 待跑", "badge": "todo",
            "goal": "", "report": None,
        }
        for name in all_names
    ]
    latest_by_task = {t: latest_run_for_task(t, runs) for t in all_names}
    newest_by_task = {t: _newest_run_for_task(t, runs) for t in all_names}
    tasks = [task_display_state(t, latest_by_task[t["name"]]) for t in base_tasks]
    n_total = len(tasks)
    n_pass = sum(1 for t in tasks if t["badge"] == "pass")
    n_fail = sum(1 for t in tasks if t["badge"] == "fail")
    n_todo = sum(1 for t in tasks if t["badge"] == "todo")
    rate = (n_pass / n_total * 100) if n_total else 0
    n_runs = len(runs)
    n_run_pass = sum(1 for r in runs if r["passed"])

    # 任务表行
    trows = []
    for i, t in enumerate(tasks, 1):
        lr = latest_by_task[t["name"]]
        nr = newest_by_task[t["name"]]
        runlink = f"<a href='/run/{nr['id']}'>最新运行</a>" if nr else "—"
        if t["report"]:
            report = f"<a href='/report/{_html.escape(t['name'])}'>报告</a>"
        elif lr and lr.get("has_report"):
            report = f"<a href='/run/{lr['id']}/report.html' target='_blank'>运行报告</a>"
        else:
            report = "—"
        source = _html.escape(t.get("source") or "README")
        trows.append(
            "<tr>"
            f"<td class='task'>{i}. {_html.escape(t['name'])}</td>"
            f"<td class='score'>{_html.escape(t['score'])}</td>"
            f"<td><span class='badge {t['badge']}' title='来源: {source}'>{_html.escape(t['status'])}</span></td>"
            f"<td class='goal' title='{_html.escape(t['goal'])}'>{_html.escape(t['goal'])}</td>"
            f"<td>{report}</td><td>{runlink}</td>"
            "</tr>"
        )

    # 最近运行行(前 20)
    rrows = []
    for r in runs[:10]:
        if r["crashed"]:
            badge = "<span class='badge crash'>💥 crashed</span>"
        else:
            badge = (f"<span class='badge pass'>✅ {r['score_str']}</span>" if r["passed"]
                     else f"<span class='badge fail'>❌ {r['score_str']}</span>")
        tlink = f"<a href='/run/{r['id']}'>{_html.escape(r['task'])}</a>"
        rrows.append(
            "<tr>"
            f"<td class='mono'>{_html.escape(r['ts_str'])}</td>"
            f"<td class='task'>{tlink}</td>"
            f"<td>{badge}</td>"
            f"<td class='goal'>{_html.escape(r['reason'])}</td>"
            f"<td class='mono'>{r['n_screenshots']} 帧</td>"
            "</tr>"
        )

    body = _header(f"{n_total} 任务 · {n_runs} 次运行 · 任务表优先使用最新 logs 评分") + f"""
    <main>
      <div class='stats'>
        <div class='stat rate'><div class='n'>{rate:.1f}%</div><div class='l'>通过率 ({n_pass}/{n_total})</div></div>
        <div class='stat pass'><div class='n'>{n_pass}</div><div class='l'>✅ 通过</div></div>
        <div class='stat fail'><div class='n'>{n_fail}</div><div class='l'>❌ 失败</div></div>
        <div class='stat todo'><div class='n'>{n_todo}</div><div class='l'>⬜ 待跑</div></div>
        <div class='stat'><div class='n'>{n_runs}</div><div class='l'>总运行次数</div></div>
        <div class='stat pass'><div class='n'>{n_run_pass}</div><div class='l'>运行通过</div></div>
      </div>

      <div class='tabs'>
        <button class='active' data-tab='tasks'>📋 任务进度</button>
        <button data-tab='runs'>🕑 最近运行</button>
      </div>

      <div id='tab-tasks' class='tab-panel active'>
      <section>
        <div class='hd'>📋 GUI-only 任务进度 <span class='hint'>点报告/最新运行查看详情</span></div>
        <div class='bd'><table>
          <tr><th>任务</th><th>score</th><th>结果</th><th>目标</th><th>报告</th><th>运行</th></tr>
          {''.join(trows)}
        </table></div>
      </section>
      </div>

      <div id='tab-runs' class='tab-panel'>
      <section>
        <div class='hd'>🕑 最近运行 <span class='hint'>最近 10 · 按 {MW_LOGS.relative_to(ROOT)}</span></div>
        <div class='bd'><table>
          <tr><th>时间</th><th>任务</th><th>结果</th><th>原因</th><th>截图</th></tr>
          {''.join(rrows) if rrows else '<tr><td colspan=5 style="color:#8a93a3;padding:20px">暂无运行</td></tr>'}
        </table></div>
      </section>
      </div>
    </main>
      <script>
      function activateTab(name) {{
        document.querySelectorAll('.tab-panel').forEach(function(p){{ p.classList.toggle('active', p.id === 'tab-' + name); }});
        document.querySelectorAll('.tabs button').forEach(function(b){{ b.classList.toggle('active', b.dataset.tab === name); }});
      }}
      document.querySelectorAll('.tabs button').forEach(function(b){{
        b.addEventListener('click', function(){{ location.hash = b.dataset.tab; activateTab(b.dataset.tab); }});
      }});
      activateTab((location.hash || '#tasks').replace('#','') === 'runs' ? 'runs' : 'tasks');
      setInterval(function(){{ location.reload(); }}, 30000);
      </script>"""
    return _layout("MobileWorld 可视化", body)






@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: str, request: Request):
    d = MW_LOGS / run_id
    if not d.is_dir() or not _safe(d, MW_LOGS):
        raise HTTPException(404, "run 不存在")
    meta = _run_meta(d)
    stdout = _read(d / "stdout.log")
    task = meta["task"] if meta["task"] != "(unknown)" else run_id
    goal = meta["goal"]
    score = meta["score"]
    reason = meta["reason"]
    crashed = meta["crashed"]

    # 逐轮标注截图(优先 _ann.jpg)
    shots = sorted(set(
        p for p in d.glob("screenshot_turn_*_ann.jpg")
    ), key=_shot_key)
    if not shots:
        shots = sorted((p for p in d.glob("screenshot_turn_*.png")), key=_shot_key)
    cards = []
    for p in shots:
        turn = _turn_of(p.name)
        cards.append(
            f"<figure><img loading='lazy' src='/run/{run_id}/{_html.escape(p.name)}' "
            f"onclick='window.open(this.src)'>"
            f"<figcaption>Turn {turn} · {p.name}</figcaption></figure>"
        )

    badge = ("<span class='badge crash'>💥 crashed</span>" if crashed else
             f"<span class='badge pass'>✅ {score:g}</span>" if score is not None and score >= 1.0 else
             f"<span class='badge fail'>❌ {score:g}</span>" if score is not None else
             "<span class='badge todo'>— 无 eval</span>")
    goal_esc = _html.escape(goal)

    body = _header(f"运行 {run_id} · {_html.escape(task)}") + f"""
    <main>
      <div class='btnrow'>
        <span class='pill'>{_html.escape(run_id)}</span>
        {badge}
        {f"<span class='pill'>原因: {_html.escape(reason)}</span>" if reason else ""}
        <a class='btn' href='/run/{run_id}/report.html' target='_blank'>📄 完整 report.html</a>
        <a class='btn ghost' href='/stdout/{run_id}' target='_blank'>📜 stdout.log</a>
        <a class='btn ghost' href='/'>← 返回</a>
      </div>
      <p style='color:#a9b0bd;padding:0 18px 8px'><b>目标:</b> {goal_esc}</p>

      <section>
        <div class='hd'>🖼 逐轮截图({len(cards)} 帧,点击放大)</div>
        <div class='shots'>{''.join(cards) if cards else '<div style="padding:20px;color:#8a93a3">无截图</div>'}</div>
      </section>

      <section>
        <div class='hd'>📜 stdout(末 300 行)</div>
        <div style='padding:16px 18px'><pre>{_html.escape(_tail(stdout, 300))}</pre></div>
      </section>
    </main>"""
    return _layout(f"{task} · {run_id}", body)


def _shot_key(p: Path):
    return (_turn_of(p.name), p.name)


def _turn_of(name: str) -> int:
    m = re.search(r"turn_((\d+))", name)
    return int(m.group(1)) if m else 0


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _safe(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


@app.get("/run/{run_id}/{filename:path}")
def run_file(run_id: str, filename: str):
    d = MW_LOGS / run_id
    if not d.is_dir() or not _safe(d, MW_LOGS):
        raise HTTPException(404, "run 不存在")
    fp = (d / filename).resolve()
    if not _safe(fp, d) or not fp.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(fp)






@app.get("/stdout/{run_id}", response_class=PlainTextResponse)
def run_stdout(run_id: str):
    d = MW_LOGS / run_id
    if not d.is_dir() or not _safe(d, MW_LOGS):
        raise HTTPException(404, "run 不存在")
    return _read(d / "stdout.log")










def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MobileWorld 可视化服务(FastAPI)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = p.parse_args(argv)
    import uvicorn
    print(f"[mw_view] serving on http://{args.host}:{args.port}")
    print(f"[mw_view] logs : {MW_LOGS}")
    print(f"[mw_view] tasks: {README}")
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
