"""HTML helpers for orchestrator and non-UI report sections."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .html_utils import _safe
from .models import ReportStep

_PROG_KIND_BADGE = {
    "navigation": "milestone-badge-navigation", "filter": "milestone-badge-filter",
    "action": "milestone-badge-action", "read": "milestone-badge-collection",
    "data_query": "milestone-badge-collection",
}

# {var[field]} data-flow template (mirrors the runner's program.TEMPLATE_RE): the report uses it
# to show what each read captured and to resolve action targets the way the runner did at runtime.
_PROG_TEMPLATE_RE = re.compile(r"\{(\w+)\[([^\]]+)\]\}")


def _program_run_items(stmts: list) -> list[dict]:
    """Flatten run statements from a DSL program, preserving source order."""
    out: list[dict] = []
    for s in stmts or []:
        if not isinstance(s, dict):
            continue
        op = s.get("op", "run")
        if op == "run":
            out.append(s)
        elif op == "if":
            out.extend(_program_run_items(s.get("then", [])))
            out.extend(_program_run_items(s.get("otherwise", [])))
    return out


def _program_run_meta(record: dict, run_items: list[dict]) -> dict:
    var = str(record.get("var") or "")
    name = str(record.get("name") or "")
    if var:
        for item in run_items:
            if str(item.get("var") or "") == var:
                return item
    if name:
        for item in run_items:
            if str(item.get("name") or "") == name:
                return item
    return {}


def _attach_non_ui_screenshots(orchestrator: dict | None, run_dir: Path) -> None:
    """Attach screenshot_read_N files to non-UI run_log rows for report rendering."""
    if not isinstance(orchestrator, dict):
        return
    run_log = orchestrator.get("run_log")
    if not isinstance(run_log, list):
        return
    run_items = _program_run_items((orchestrator.get("program") or {}).get("statements") or [])

    def _shot_index(path: Path) -> int:
        m = re.search(r"screenshot_read_(\d+)\.png$", path.name)
        return int(m.group(1)) if m else 10**9

    shots = sorted(run_dir.glob("screenshot_read_*.png"), key=_shot_index)
    shot_i = 0
    for record in run_log:
        if not isinstance(record, dict):
            continue
        meta = _program_run_meta(record, run_items)
        if meta.get("kind") not in {"read", "data_query"}:
            continue
        if record.get("observation_url"):
            continue
        if shot_i < len(shots):
            record["observation_url"] = shots[shot_i].name
            shot_i += 1


def _synthetic_non_ui_steps(
    orchestrator: dict | None,
    run_dir: Path,
    *,
    start_index: int,
    existing: set[tuple[str, str]],
) -> list[ReportStep]:
    """Build turn-like report rows for archived logs where non-UI primitives predate turn persistence."""
    if not isinstance(orchestrator, dict):
        return []
    run_log = orchestrator.get("run_log")
    if not isinstance(run_log, list):
        return []
    run_items = _program_run_items((orchestrator.get("program") or {}).get("statements") or [])
    steps: list[ReportStep] = []
    next_index = start_index
    for record in run_log:
        if not isinstance(record, dict):
            continue
        meta = _program_run_meta(record, run_items)
        kind = str(meta.get("kind") or "")
        if kind not in {"read", "data_query"}:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        name = str(record.get("name") or meta.get("name") or "")
        var = str(record.get("var") or meta.get("var") or "")
        key = (var, name)
        if key in existing:
            continue
        reads = result.get("reads") if isinstance(result.get("reads"), dict) else {}
        non_ui = {
            "kind": kind,
            "name": name,
            "var": var,
            "returns": meta.get("returns") or list(reads.keys()),
            "read_spec": meta.get("read_spec") or "",
            "sql": meta.get("sql") or "",
            "data_scope": meta.get("data_scope") or "",
            "reads": reads,
            "summary": result.get("summary") or "",
            "completed": bool(result.get("completed")) and not bool(result.get("failed")),
            "failed": bool(result.get("failed")),
            "observation_url": record.get("observation_url") or "",
        }
        shot_name = str(non_ui.get("observation_url") or "")
        shot_path = run_dir / shot_name if shot_name else None
        shot_url = shot_path.name if shot_path and shot_path.exists() else ""
        completed = bool(non_ui["completed"])
        steps.append(
            ReportStep(
                label=f"Turn {next_index}",
                action_type=kind,
                x=None,
                y=None,
                description=name,
                annotated_before_url=shot_url,
                annotated_full_url=shot_url,
                raw_screenshot_url=shot_url,
                status="✓ non-UI" if completed else "✗ non-UI",
                milestone_id=var or f"non_ui_{next_index}",
                milestone_kind=kind,
                instruction="",
                summary=str(non_ui.get("summary") or ""),
                operation_mode="non_interactive",
                non_ui=non_ui,
            )
        )
        next_index += 1
    return steps


def _pretty_non_ui_value(value: object) -> str:
    text = str(value if value is not None else "")
    try:
        parsed = json.loads(text)
    except Exception:
        return text
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _render_non_ui_detail(non_ui: dict) -> str:
    kind = str(non_ui.get("kind") or "non_ui")
    mode = "非交互"
    sql = str(non_ui.get("sql") or "")
    scope = str(non_ui.get("data_scope") or "")
    read_spec = str(non_ui.get("read_spec") or "")
    reads = non_ui.get("reads") if isinstance(non_ui.get("reads"), dict) else {}
    returns = [str(v) for v in (non_ui.get("returns") or []) if str(v)]
    fields = returns or list(reads.keys())
    sql_html = ""
    if sql:
        scope_html = f' <span style="color:#94a3b8">scope={_safe(scope)}</span>' if scope else ""
        sql_html = (
            f'<div class="nonui-sql"><span class="nonui-label">SQL{scope_html}</span>'
            f'<pre class="nonui-code">{_safe(sql)}</pre></div>'
        )
    spec_html = (
        f'<div class="nonui-sql"><span class="nonui-label">SPEC</span>'
        f'<pre class="nonui-code">{_safe(read_spec)}</pre></div>'
        if read_spec else ""
    )
    read_rows = []
    for field in fields:
        read_rows.append(
            f'<div class="nonui-read"><span class="nonui-key">{_safe(field)}</span>'
            f'<pre class="nonui-val">{_safe(_pretty_non_ui_value(reads.get(field, "")))}</pre></div>'
        )
    reads_html = f'<div class="nonui-reads">{"".join(read_rows)}</div>' if read_rows else ""
    return (
        f'<div class="nonui-detail">'
        f'<div class="nonui-title">{mode} · {_safe(kind)}</div>'
        f'{sql_html}{spec_html}{reads_html}'
        f'</div>'
    )


def _render_non_ui_log(orchestrator: dict, run_items: list[dict]) -> str:
    rows: list[str] = []
    for idx, record in enumerate(orchestrator.get("run_log") or [], start=1):
        if not isinstance(record, dict):
            continue
        meta = _program_run_meta(record, run_items)
        kind = str(meta.get("kind") or "")
        if kind not in {"read", "data_query"}:
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        completed = bool(result.get("completed")) and not bool(result.get("failed"))
        status_cls = "nonui-ok" if completed else "nonui-fail"
        status_text = "completed" if completed else "failed"
        badge = _PROG_KIND_BADGE.get(kind, "milestone-badge-default")
        name = str(record.get("name") or meta.get("name") or "")
        var = str(record.get("var") or meta.get("var") or "")
        var_html = f'<span class="prog-var">{_safe(var)} =</span> ' if var else ""
        summary = str((result or {}).get("summary") or "")
        summary_html = f'<div class="nonui-summary">{_safe(summary)}</div>' if summary else ""
        sql = str(meta.get("sql") or "")
        scope = str(meta.get("data_scope") or "")
        sql_html = ""
        if kind == "data_query" and sql:
            scope_html = f' <span style="color:#94a3b8">scope={_safe(scope)}</span>' if scope else ""
            sql_html = (
                f'<div class="nonui-sql"><span class="nonui-label">SQL{scope_html}</span>'
                f'<pre class="nonui-code">{_safe(sql)}</pre></div>'
            )
        reads = result.get("reads") if isinstance(result.get("reads"), dict) else {}
        read_rows = []
        for field in (meta.get("returns") or list(reads.keys()) or []):
            val = reads.get(field, "")
            read_rows.append(
                f'<div class="nonui-read"><span class="nonui-key">{_safe(str(field))}</span>'
                f'<pre class="nonui-val">{_safe(_pretty_non_ui_value(val))}</pre></div>'
            )
        reads_html = f'<div class="nonui-reads">{"".join(read_rows)}</div>' if read_rows else ""
        obs = str(record.get("observation_url") or "")
        shot_html = (
            f'<div class="nonui-shot"><img src="{_safe(obs)}" onclick="zoomImg(\'{_safe(obs)}\')" '
            f'alt="非 UI 读取帧"></div>'
            if obs else ""
        )
        rows.append(
            f'<div class="nonui-row">'
            f'<div class="nonui-main">'
            f'<div class="nonui-head">'
            f'<span class="nonui-n">#{idx}</span>'
            f'<span class="nonui-name">{var_html}{_safe(name)}</span>'
            f'<span class="milestone-badge {badge}">{_safe(kind)}</span>'
            f'<span class="nonui-status {status_cls}">{status_text}</span>'
            f'</div>'
            f'{summary_html}{sql_html}{reads_html}'
            f'</div>{shot_html}</div>'
        )
    if not rows:
        return ""
    return (
        f'<div class="nonui-log">'
        f'<div class="nonui-title">非 UI 执行记录</div>'
        f'{"".join(rows)}'
        f'</div>'
    )


def _render_program_section(orchestrator: dict | None) -> str:
    """Orchestrator mode: render the decomposed DSL program as its OWN section, #0 编排.

    decompose is a distinct stage now (goal → run/if/finish program), not folded into turn 1
    and not lumped with the Router / 模型配置 header rows. It gets its own milestone-style card
    (#0, before the executed milestones) showing the run/if/finish structure vertically.
    Empty (DAG mode) → no section."""
    if not orchestrator:
        return ""
    stmts = (orchestrator.get("program") or {}).get("statements") or []
    if not stmts:
        return ""
    run_items = _program_run_items(stmts)

    # var -> {field: value} captured by each completed read (runner mirrors interp.run_log into
    # context.orchestrator). Lets the report show WHAT a read got and resolve {var[field]} action
    # targets — a pure read has no turn/milestone, so without this the report only had the static
    # program structure, never the values it read or where they flowed.
    env: dict[str, dict] = {}
    for r in (orchestrator.get("run_log") or []):
        reads = (r.get("result") or {}).get("reads") or {}
        if r.get("var") and reads:
            env[r["var"]] = reads

    def _resolve(text: str) -> str:
        """Substitute {var[field]} from env (as the runner did at execute time); keep the raw
        ref when unresolved so the data-flow wiring stays visible."""
        def _sub(m: "re.Match[str]") -> str:
            vals = env.get(m.group(1))
            return vals.get(m.group(2).strip().strip("'\""), m.group(0)) if vals else m.group(0)
        return _PROG_TEMPLATE_RE.sub(_sub, text or "")

    counter = [0]

    def _run_row(s: dict) -> str:
        counter[0] += 1
        kind = s.get("kind", "action")
        badge = _PROG_KIND_BADGE.get(kind, "milestone-badge-default")
        var = s.get("var")
        var_html = f'<span class="prog-var">{_safe(var)} =</span> ' if var else ""
        name = s.get("name", "")
        # show the authored name (template intact = the program), then the runtime-resolved target
        # next to it when {var[field]} filled (so 编辑 {r[实际名称]} → lucas-10003 is visible)
        resolved = _resolve(name)
        resolved_html = (
            f'<span class="prog-resolved">▸ {_safe(resolved)}</span>' if resolved != name else ""
        )
        ret = [r for r in (s.get("returns") or []) if r]
        if kind in {"read", "data_query"} and ret:
            vals = env.get(var or "") or {}
            verb = "查" if kind == "data_query" else "读"
            shown = "、".join(
                f"{_safe(f)}={_safe(vals[f])}" if vals.get(f) else _safe(f) for f in ret
            )
            ret_html = f'<span class="prog-ret">→ {verb} {shown}</span>'
        else:
            ret_html = ""
        return (
            f'<div class="prog-step">'
            f'<span class="prog-n">{counter[0]}</span>'
            f'<span class="prog-name">{var_html}{_safe(name)}</span>{resolved_html}'
            f'<span class="milestone-badge {badge}">{_safe(kind)}</span>{ret_html}'
            f'</div>'
        )

    def _walk(items: list) -> list[str]:
        out = []
        for s in items:
            op = s.get("op", "run")
            if op == "run":
                out.append(_run_row(s))
            elif op == "if":
                cond = s.get("cond", {})
                c = (f'<span class="prog-condvar">{_safe(cond.get("var",""))}[{_safe(cond.get("field",""))}]</span>'
                     f' {_safe(cond.get("cmp","=="))} '
                     f'<span class="prog-condval">{_safe(cond.get("value",""))}</span>')
                then_html = "".join(_walk(s.get("then", []))) or '<div class="prog-step prog-empty">—</div>'
                else_html = "".join(_walk(s.get("otherwise", []))) or '<div class="prog-step prog-empty">—</div>'
                out.append(
                    f'<div class="prog-if">'
                    f'<div class="prog-cond"><span class="prog-kw">if</span> {c} <span class="prog-kw">:</span></div>'
                    f'<div class="prog-branch">{then_html}</div>'
                    f'<div class="prog-cond"><span class="prog-kw">else :</span></div>'
                    f'<div class="prog-branch prog-branch-else">{else_html}</div>'
                    f'</div>'
                )
            elif op == "finish":
                out.append(f'<div class="prog-finish">↩ finish「{_safe(s.get("message", ""))}」</div>')
        return out

    body = "".join(_walk(stmts))
    goal = _safe((orchestrator.get("program") or {}).get("goal") or "")
    input_html = (
        f'<div class="prog-input"><span class="prog-input-label">输入</span>{goal}'
        f'<span class="prog-input-arrow">↓ 分解为</span></div>'
    ) if goal else ""
    return (
        f'<div class="milestone prog-section" id="ms-orchestrate">'
        f'<div class="milestone-header">'
        f'<h2>#0</h2>'
        f'<span class="milestone-name">编排 · decompose → DSL program</span>'
        f'<span class="milestone-badge milestone-badge-default">program</span>'
        f'</div>'
        f'<div class="prog-body">{input_html}{body}</div>'
        f'</div>'
    )
