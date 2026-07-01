"""WebArena 跑测可视化服务 —— 单文件 FastAPI 仪表盘(仿 benchmark/mobileworld/server.py)。

两条数据源:
  - `logs/gui_agent/webarena/browser/<ts>/` 每次运行的完整产物(逐轮截图 + stdout + report.html + context.json)
  - `benchmark/webarena/README.md` 的 shopping_admin 任务进度表 + `reports/<task>.html` inline 报告

WebArena 评分从 context.json 的 `webarena.eval_result`(score / task_id / evaluators_results)+
`webarena.agent_response`(status / error_details)读取,并按 task_id 关联回 README 任务表。

提供:
  GET /                         首页:通过率统计 + 任务表(README 叠加最新 logs 评分) + 最近运行
  GET /run/{run_id}             单次运行详情:逐轮截图 + evaluator 分项 + agent 状态/错误 + stdout + report 入口
  GET /run/{run_id}/{filename}  run 目录内文件(截图/log/report.html)
  GET /report/{task}            benchmark/webarena/reports/<task>.html(自包含 inline 报告)
  GET /stdout/{run_id}          原始 stdout.log(纯文本)
  GET /api/runs                 运行列表 JSON(供前端轮询自动刷新)

启动:bin/wa_view  或  uv run python -m benchmark.webarena.server [--port 8100] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parents[2]
WA_DIR = Path(__file__).resolve().parent                           # benchmark/webarena/
WA_LOGS = ROOT / "logs" / "gui_agent" / "webarena" / "browser"
README = WA_DIR / "README.md"
REPORTS = WA_DIR / "reports"
SA_RUN = ROOT / "webarena-verified" / "output" / "sa_run"          # official eval_result.json per task

app = FastAPI(title="WebArena 可视化")


# --------------------------------------------------------------------------- #
# 数据解析
# --------------------------------------------------------------------------- #
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _parse_ts(name: str):
    for fmt in ("%Y%m%d_%H%M%S", "%Y-%m-%d_%H-%M-%S"):
        try:
            return datetime.strptime(name, fmt)
        except ValueError:
            continue
    return None


def _agent_from_eval(evrs: list[dict]) -> tuple[str | None, str | None, str | None]:
    """从 AgentResponseEvaluator 的 actual(JSON 字符串)里挖 agent status / error / task_type。"""
    for er in evrs:
        if er.get("evaluator_name") == "AgentResponseEvaluator":
            try:
                a = json.loads(er.get("actual") or "{}")
                return a.get("status"), a.get("error_details"), a.get("task_type")
            except Exception:  # noqa: BLE001
                pass
    return None, None, None


def _run_meta(d: Path) -> dict:
    """单次 run 的元数据。主源 context.json 的 webarena 子结构;stdout 补崩溃判定。"""
    goal, score, task_id, task_type = None, None, None, None
    agent_status, error, evaluators, turns = None, None, {}, 0
    ctx = d / "context.json"
    if ctx.is_file():
        try:
            cj = json.loads(ctx.read_text(encoding="utf-8", errors="replace"))
            goal = cj.get("goal")
            turns = len(cj.get("turns") or []) if isinstance(cj.get("turns"), list) else 0
            wa = cj.get("webarena") or {}
            ev = wa.get("eval_result") or {}
            score = ev.get("score")
            task_id = ev.get("task_id")
            evrs = ev.get("evaluators_results") or []
            evaluators = {er.get("evaluator_name"): er.get("status") for er in evrs}
            ar = wa.get("agent_response") or {}
            agent_status = ar.get("status")
            error = ar.get("error_details")
            task_type = ar.get("task_type")
            if agent_status is None:
                agent_status, error, task_type = _agent_from_eval(evrs)
        except Exception:  # noqa: BLE001
            pass
    stdout = _read(d / "stdout.log")
    ts = _parse_ts(d.name)
    n_shots = len(list(d.glob("screenshot_turn_*_ann.jpg"))) or len(list(d.glob("screenshot_turn_*.png")))
    # timed-out/killed sweep runs: context.json exists but no eval + last line hangs at settle
    hung = bool(re.search(r"CDP settle 异常，回退视觉", stdout)) and score is None
    return {
        "id": d.name,
        "ts": ts,
        "ts_str": ts.strftime("%Y-%m-%d %H:%M:%S") if ts else d.name,
        "task_id": task_id,
        "task": str(task_id) if task_id is not None else "(unknown)",
        "goal": (goal or "")[:160],
        "score": score,
        "score_str": f"{score:g}" if score is not None else "—",
        "task_type": task_type or "",
        "agent_status": agent_status or "",
        "error": (error or "")[:200],
        "evaluators": evaluators,
        "turns": turns,
        "passed": (score is not None and score >= 1.0),
        "crashed": ("Traceback" in stdout) and score is None,
        "hung": hung,
        "has_report": (d / "report.html").exists(),
        "n_screenshots": n_shots,
    }


def parse_runs() -> list[dict]:
    if not WA_LOGS.exists():
        return []
    runs = [_run_meta(d) for d in WA_LOGS.iterdir() if d.is_dir()]
    # keep only runs that carry a WebArena eval or a goal (skip unrelated agent-loop dirs)
    runs = [r for r in runs if r["task_id"] is not None or r["goal"]]
    runs.sort(key=lambda r: (r["ts"] or datetime.min), reverse=True)
    return runs


def _status_badge(status: str) -> str:
    if "✅" in status:
        return "pass"
    if "❌" in status:
        return "fail"
    return "todo"


def parse_readme_tasks() -> list[dict]:
    """解析 README.md 的 shopping_admin 表 → [{name(task_id), score, status, goal, report}]。"""
    text = _read(README)
    rows: list[dict] = []
    in_table = False
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if s.startswith("|") and "task" in low and "score" in low:  # header
            in_table = True
            continue
        if re.match(r"^\|\s*-{2,}", s):  # separator
            continue
        if not in_table or not s.startswith("|"):
            if in_table and s and not s.startswith("|"):
                in_table = False
            continue
        parts = [p.strip() for p in s.strip("|").split("|")]
        if len(parts) < 5:
            continue
        name, score, status, goal, report = parts[0], parts[1], parts[2], parts[3], parts[4]
        if name.lower() in ("task", "任务") or name.startswith("---"):
            continue
        m = re.search(r"\((reports/[^)]+)\)", report)
        rows.append({
            "name": name, "score": score, "status": status,
            "badge": _status_badge(status), "goal": goal,
            "report": m.group(1) if m else None,
        })
    return rows


def latest_run_for_task(task: str, runs: list[dict]) -> dict | None:
    for r in runs:
        if r["task"] == str(task):
            return r
    return None


def task_display_state(task: dict, latest: dict | None) -> dict:
    """README 是策展基线,可能滞后;有带 eval 的最新 run 时用它覆盖 score/status。"""
    out = dict(task)
    if latest is None or latest.get("score") is None:
        out["source"] = "README"
        return out
    score = latest["score"]
    out["score"] = latest.get("score_str") or f"{score:g}"
    if latest.get("passed"):
        out["status"], out["badge"] = "✅ 通过", "pass"
    elif latest.get("crashed") or latest.get("hung"):
        out["status"], out["badge"] = "💥 崩溃/挂死", "crash"
    else:
        out["status"], out["badge"] = "❌ 失败", "fail"
    out["source"] = f"logs/{latest['id']}"
    return out


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
pre { background:#0b0d11; padding:16px; border-radius:8px; overflow:auto; font-size:12px; line-height:1.5;
      border:1px solid #20242c; max-height:70vh; }
.btnrow { padding:14px 18px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
.btn { background:#2266cc; color:#fff; padding:7px 14px; border-radius:8px; font-size:13px; }
.btn.ghost { background:#20242c; color:#cdd3dd; }
"""


def _layout(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_html.escape(title)}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def _header(sub: str) -> str:
    return (
        "<header><h1>🌐 WebArena 跑测可视化</h1>"
        f"<div class='sub'>{_html.escape(sub)}</div></header>"
    )


def _eval_pills(evaluators: dict) -> str:
    out = []
    for name, st in (evaluators or {}).items():
        short = name.replace("Evaluator", "")
        cls = "pass" if st == "success" else "fail"
        out.append(f"<span class='badge {cls}' title='{_html.escape(name)}'>{_html.escape(short)}: {_html.escape(str(st))}</span>")
    return " ".join(out)


def _run_badge(r: dict) -> str:
    if r["crashed"]:
        return "<span class='badge crash'>💥 crashed</span>"
    if r["hung"]:
        return "<span class='badge crash'>⏳ settle 挂死</span>"
    if r["passed"]:
        return f"<span class='badge pass'>✅ {r['score_str']}</span>"
    if r["score"] is not None:
        return f"<span class='badge fail'>❌ {r['score_str']}</span>"
    return "<span class='badge todo'>— 无 eval</span>"


@app.get("/", response_class=HTMLResponse)
def index():
    runs = parse_runs()
    raw_tasks = parse_readme_tasks()
    latest_by_task = {t["name"]: latest_run_for_task(t["name"], runs) for t in raw_tasks}
    tasks = [task_display_state(t, latest_by_task[t["name"]]) for t in raw_tasks]
    n_total = len(tasks)
    n_pass = sum(1 for t in tasks if t["badge"] == "pass")
    n_fail = sum(1 for t in tasks if t["badge"] in ("fail", "crash"))
    n_todo = sum(1 for t in tasks if t["badge"] == "todo")
    rate = (n_pass / n_total * 100) if n_total else 0
    n_runs = len(runs)
    n_run_pass = sum(1 for r in runs if r["passed"])

    trows = []
    for i, t in enumerate(tasks, 1):
        lr = latest_by_task[t["name"]]
        runlink = f"<a href='/run/{lr['id']}'>最新运行</a>" if lr else "—"
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

    rrows = []
    for r in runs[:40]:
        tlabel = r["task"] if r["task"] != "(unknown)" else r["id"]
        tlink = f"<a href='/run/{r['id']}'>{_html.escape(tlabel)}</a>"
        detail = r["error"] or (r["goal"] if not r["passed"] else "")
        rrows.append(
            "<tr>"
            f"<td class='mono'>{_html.escape(r['ts_str'])}</td>"
            f"<td class='task'>{tlink}</td>"
            f"<td>{_run_badge(r)}</td>"
            f"<td><span class='pill'>{_html.escape(r['agent_status'] or '—')}</span></td>"
            f"<td class='goal'>{_html.escape(detail)}</td>"
            f"<td class='mono'>{r['turns']}t · {r['n_screenshots']}帧</td>"
            "</tr>"
        )

    body = _header(f"{n_total} 归档任务 · {n_runs} 次运行 · 任务表优先用最新 logs 评分") + f"""
    <main>
      <div class='stats'>
        <div class='stat rate'><div class='n'>{rate:.1f}%</div><div class='l'>归档通过率 ({n_pass}/{n_total})</div></div>
        <div class='stat pass'><div class='n'>{n_pass}</div><div class='l'>✅ 通过</div></div>
        <div class='stat fail'><div class='n'>{n_fail}</div><div class='l'>❌ 失败</div></div>
        <div class='stat todo'><div class='n'>{n_todo}</div><div class='l'>⬜ 未评</div></div>
        <div class='stat'><div class='n'>{n_runs}</div><div class='l'>总运行次数</div></div>
        <div class='stat pass'><div class='n'>{n_run_pass}</div><div class='l'>运行通过</div></div>
      </div>

      <section>
        <div class='hd'>📋 shopping_admin 归档任务 <span class='hint'>README 表 · 有最新 run 时覆盖评分</span></div>
        <div class='bd'><table>
          <tr><th>任务</th><th>score</th><th>结果</th><th>目标</th><th>报告</th><th>运行</th></tr>
          {''.join(trows) if trows else '<tr><td colspan=6 style="color:#8a93a3;padding:20px">README 无任务表</td></tr>'}
        </table></div>
      </section>

      <section>
        <div class='hd'>🕑 最近运行 <span class='hint'>前 40 · {WA_LOGS.relative_to(ROOT)} · 每 5s 自动刷新</span></div>
        <div class='bd'><table>
          <tr><th>时间</th><th>task</th><th>结果</th><th>agent</th><th>错误/目标</th><th>轮次/帧</th></tr>
          {''.join(rrows) if rrows else '<tr><td colspan=6 style="color:#8a93a3;padding:20px">暂无运行</td></tr>'}
        </table></div>
      </section>
    </main>
    <script>
    (() => {{
      const initial = {{count: {n_runs}, latest: {json.dumps(runs[0]["id"] if runs else "")}}};
      async function tick() {{
        try {{
          const res = await fetch('/api/runs?ts=' + Date.now(), {{cache: 'no-store'}});
          if (!res.ok) return;
          const data = await res.json();
          const latest = data.length ? data[0].id : '';
          if (data.length !== initial.count || latest !== initial.latest) window.location.reload();
        }} catch (err) {{}}
      }}
      window.setInterval(tick, 5000);
    }})();
    </script>"""
    return _layout("WebArena 可视化", body)


def _shot_key(p: Path):
    return (_turn_of(p.name), p.name)


def _turn_of(name: str) -> int:
    m = re.search(r"turn_(\d+)", name)
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


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: str):
    d = WA_LOGS / run_id
    if not d.is_dir() or not _safe(d, WA_LOGS):
        raise HTTPException(404, "run 不存在")
    meta = _run_meta(d)
    stdout = _read(d / "stdout.log")

    shots = sorted(set(d.glob("screenshot_turn_*_ann.jpg")), key=_shot_key)
    if not shots:
        shots = sorted(d.glob("screenshot_turn_*.png"), key=_shot_key)
    cards = []
    for p in shots:
        cards.append(
            f"<figure><img loading='lazy' src='/run/{run_id}/{_html.escape(p.name)}' "
            f"onclick='window.open(this.src)'>"
            f"<figcaption>Turn {_turn_of(p.name)} · {p.name}</figcaption></figure>"
        )

    report_btn = (f"<a class='btn' href='/run/{run_id}/report.html' target='_blank'>📄 完整 report.html</a>"
                  if meta["has_report"] else "")
    err_line = (f"<p style='color:#ff9b9b;padding:0 18px 8px'><b>错误:</b> {_html.escape(meta['error'])}</p>"
                if meta["error"] else "")

    body = _header(f"运行 {run_id} · task {_html.escape(meta['task'])}") + f"""
    <main>
      <div class='btnrow'>
        <span class='pill'>{_html.escape(run_id)}</span>
        {_run_badge(meta)}
        <span class='pill'>{_html.escape(meta['task_type'] or '?')}</span>
        <span class='pill'>agent: {_html.escape(meta['agent_status'] or '—')}</span>
        {_eval_pills(meta['evaluators'])}
        {report_btn}
        <a class='btn ghost' href='/stdout/{run_id}' target='_blank'>📜 stdout.log</a>
        <a class='btn ghost' href='/'>← 返回</a>
      </div>
      <p style='color:#a9b0bd;padding:0 18px 8px'><b>目标:</b> {_html.escape(meta['goal'])}</p>
      {err_line}

      <section>
        <div class='hd'>🖼 逐轮截图({len(cards)} 帧,点击放大)</div>
        <div class='shots'>{''.join(cards) if cards else '<div style="padding:20px;color:#8a93a3">无截图</div>'}</div>
      </section>

      <section>
        <div class='hd'>📜 stdout(末 400 行)</div>
        <div style='padding:16px 18px'><pre>{_html.escape(_tail(stdout, 400))}</pre></div>
      </section>
    </main>"""
    return _layout(f"task {meta['task']} · {run_id}", body)


@app.get("/run/{run_id}/{filename:path}")
def run_file(run_id: str, filename: str):
    d = WA_LOGS / run_id
    if not d.is_dir() or not _safe(d, WA_LOGS):
        raise HTTPException(404, "run 不存在")
    fp = (d / filename).resolve()
    if not _safe(fp, d) or not fp.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(fp)


@app.get("/report/{task}", response_class=HTMLResponse)
def task_report(task: str):
    fp = REPORTS / f"{task}.html"
    if _safe(fp, REPORTS) and fp.is_file():
        return FileResponse(fp)
    latest = latest_run_for_task(task, parse_runs())
    if latest and latest.get("has_report"):
        run_fp = WA_LOGS / latest["id"] / "report.html"
        if _safe(run_fp, WA_LOGS) and run_fp.is_file():
            return FileResponse(run_fp)
    raise HTTPException(404, "无报告(任务可能还没跑过或未生成报告)")


@app.get("/stdout/{run_id}", response_class=PlainTextResponse)
def run_stdout(run_id: str):
    d = WA_LOGS / run_id
    if not d.is_dir() or not _safe(d, WA_LOGS):
        raise HTTPException(404, "run 不存在")
    return _read(d / "stdout.log")


@app.get("/api/runs")
def api_runs():
    return parse_runs()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WebArena 可视化服务(FastAPI)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8100)
    p.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = p.parse_args(argv)
    import uvicorn
    print(f"[wa_view] serving on http://{args.host}:{args.port}")
    print(f"[wa_view] logs : {WA_LOGS}")
    print(f"[wa_view] tasks: {README}")
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
