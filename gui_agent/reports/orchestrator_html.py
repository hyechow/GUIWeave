"""HTML helpers for orchestrator and non-UI report sections."""

from __future__ import annotations

import ast
import json

from gui_agent.core.config import pricing_currency

from .html_utils import _safe
from .metrics import _fmt_tokens, _sum_tokens, _token_cost
from .prompt_html import _render_module_io_html

def _field_summary(fields: list[str]) -> str:
    shown = ", ".join(fields[:12])
    suffix = f", … +{len(fields) - 12}" if len(fields) > 12 else ""
    return f"{len(fields)} fields [{shown}{suffix}]"


def _report_value(value: object, *, sample_items: int, limit: int) -> str:
    """Bound data-plane values in HTML while keeping a useful report projection."""
    items = value
    prefix = ""
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        items = value["rows"]
        prefix = "rows: "
    if isinstance(items, list) and items and all(isinstance(item, dict) for item in items):
        fields = list(dict.fromkeys(str(key) for row in items for key in row))
        summary = f"{prefix}{len(items)} records · {_field_summary(fields)}"
        if sample_items:
            sample = json.dumps(
                items[:sample_items], ensure_ascii=False, indent=2, default=str,
            )
            summary += f"\nsample (first {min(len(items), sample_items)}):\n{sample}"
        return summary
    if isinstance(items, list) and len(items) > 8:
        summary = f"{prefix}{len(items)} items"
        if sample_items:
            sample = json.dumps(items[:sample_items], ensure_ascii=False, indent=2, default=str)
            summary += f"\nsample (first {sample_items}):\n{sample}"
        return summary
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, indent=2 if sample_items else None, default=str,
    )
    if len(text) <= limit:
        return text
    if isinstance(value, dict):
        return f"object · {_field_summary(list(map(str, value)))}"
    return text[:limit - 3] + "…"


def _render_non_ui_detail(non_ui: dict) -> str:
    kind = str(non_ui.get("executor") or "non_ui")
    mode = {
        "acquire": "Acquire 集合采集",
        "read": "Read 观察绑定",
        "command": "确定性命令",
    }.get(kind, "非交互")
    outputs = non_ui.get("outputs") if isinstance(non_ui.get("outputs"), dict) else {}
    fields = list(outputs)
    evidence = [str(value) for value in (non_ui.get("evidence") or [])]
    read_rows = []
    for field in fields:
        read_rows.append(
            f'<div class="nonui-read"><span class="nonui-key">{_safe(field)}</span>'
            f'<pre class="nonui-val">{_safe(_report_value(outputs.get(field, ""), sample_items=2, limit=2400))}</pre></div>'
        )
    reads_html = f'<div class="nonui-reads">{"".join(read_rows)}</div>' if read_rows else ""
    evidence_html = (
        f'<pre class="nonui-code">{_safe(chr(10).join(evidence))}</pre>'
        if evidence else ""
    )
    return (
        f'<div class="nonui-detail">'
        f'<div class="nonui-title">{_safe(mode)}</div>'
        f'{reads_html}'
        f'{evidence_html}'
        f'</div>'
    )


def _render_orchestrator_context_reports(orchestrator: dict) -> str:
    reports = orchestrator.get("context_reports") or []
    if not reports:
        return '<div class="compat-row"><span class="compat-chip">未记录 prompt snapshot</span></div>'
    token_usage = orchestrator.get("token_usage") if isinstance(orchestrator.get("token_usage"), dict) else {}
    if not token_usage:
        token_usage = _estimate_orchestrator_token_usage(reports)
    return _render_module_io_html(reports, token_usage)


def _render_orchestrator_metrics(orchestrator: dict) -> str:
    token_usage = orchestrator.get("token_usage") if isinstance(orchestrator.get("token_usage"), dict) else {}
    estimated = False
    if not token_usage:
        token_usage = _estimate_orchestrator_token_usage(orchestrator.get("context_reports") or [])
        estimated = bool(token_usage)
    ti, to = _sum_tokens(token_usage)
    timings = orchestrator.get("timings") if isinstance(orchestrator.get("timings"), dict) else {}
    total_s = sum(float(v or 0) for v in timings.values())
    calls = int(orchestrator.get("llm_calls") or 0) or _count_prompt_calls(orchestrator.get("context_reports") or [])

    parts: list[str] = []
    if total_s > 0:
        parts.append(f"{total_s:.1f}s")
    elif calls or ti or to:
        parts.append('<span class="compat-chip">耗时未记录</span>')
    if calls:
        parts.append(f"{calls} call{'s' if calls != 1 else ''}")
    if ti or to:
        prefix = "≈" if estimated else ""
        cost_prefix = "≈"
        parts.append(f"{prefix}{_fmt_tokens(ti)}/{_fmt_tokens(to)} tok")
        parts.append(f"{cost_prefix}{pricing_currency()}{_token_cost(token_usage):.4f}")
    if not parts:
        return ""
    return f'<span class="statement-time">{" · ".join(parts)}</span>'


def _estimate_orchestrator_token_usage(reports: list[dict]) -> dict:
    input_chars = 0
    output_chars = 0
    for report in reports:
        if not isinstance(report, dict):
            continue
        if report.get("kind") == "prompt_snapshot":
            for role in report.get("roles") or []:
                if not isinstance(role, dict):
                    continue
                for part in role.get("parts") or []:
                    if isinstance(part, dict) and part.get("type") != "image":
                        input_chars += int(part.get("chars") or len(str(part.get("text") or "")))
        elif report.get("kind") == "llm_output":
            output_chars += int(report.get("chars") or len(str(report.get("raw_output") or "")))
    input_tokens = _estimate_tokens(input_chars)
    output_tokens = _estimate_tokens(output_chars)
    if not input_tokens and not output_tokens:
        return {}
    return {"orchestrator.coding": {"input": input_tokens, "output": output_tokens}}


def _count_prompt_calls(reports: list[dict]) -> int:
    return sum(1 for report in reports if isinstance(report, dict) and report.get("kind") == "prompt_snapshot")


def _estimate_tokens(chars: int) -> int:
    if chars <= 0:
        return 0
    return max(1, (chars + 3) // 4)


def has_coding_program(orchestrator: dict | None) -> bool:
    if not orchestrator:
        return False
    prog = orchestrator.get("program") or {}
    return prog.get("kind") == "coding" or bool(str(prog.get("source") or "").strip())


def _coding_statement_id(record: dict) -> str:
    """Resolve statement id from a report_run_log entry (new + legacy logs)."""
    node_id = str(record.get("node_id") or "").strip()
    if node_id:
        return node_id
    instance_id = str(record.get("instance_id") or "").strip()
    if ":" in instance_id:
        return instance_id.rsplit(":", 1)[-1]
    return instance_id


def _render_program_section(orchestrator: dict | None) -> str:
    """Render the compiled Python plan before its executed statements."""
    if not orchestrator:
        return ""
    prog0 = orchestrator.get("program") or {}
    if not has_coding_program(orchestrator):
        return ""
    return _render_coding_program_shell(orchestrator, prog0)


def _infer_coding_op(
    *,
    coding_op: str = "",
    executor: str = "",
    inputs: dict | None = None,
) -> str:
    """Resolve ctx.* op from structured runtime fields."""
    if coding_op:
        return {"gui": "reach", "write": "commit"}.get(
            str(coding_op), str(coding_op),
        )
    inputs = inputs if isinstance(inputs, dict) else {}
    if "lookup_request" in inputs:
        return "lookup"
    if "constrain_request" in inputs:
        return "constrain"
    if executor == "acquire":
        return "acquire"
    if executor == "read":
        return "read"
    if executor == "command":
        return "command"
    if executor == "interact":
        if isinstance(inputs.get("values"), dict) and inputs.get("values"):
            return "commit"
        return "reach"
    return executor or ""


def _flatten_coding_inputs(
    *,
    coding_op: str,
    coding_payload: dict | None,
    inputs: dict | None,
) -> dict:
    """Prefer coding_payload; fall back to statement.inputs / nested request objects."""
    if isinstance(coding_payload, dict) and coding_payload:
        return dict(coding_payload)
    inputs = inputs if isinstance(inputs, dict) else {}
    if not inputs:
        return {}
    for key in ("lookup_request", "constrain_request"):
        nested = inputs.get(key)
        if isinstance(nested, dict) and nested:
            return dict(nested)
    return dict(inputs)


def _coding_call_label(op: str, payload: dict | None = None) -> str:
    """Short human label for one translated ctx.* invocation."""
    payload = payload if isinstance(payload, dict) else {}
    if not op:
        return "ctx.?"
    if op == "reach":
        goal = payload.get("goal") or payload.get("task") or ""
        success = payload.get("success")
        target = payload.get("target")
        success_text = ""
        if (
            isinstance(success, dict)
            and success.get("entity")
        ):
            entity = success.get("entity")
            fields = success.get("fields") or []
            success_text = (
                f", success=collection({entity!r}, "
                f"fields={fields!r})"
            )
        target_text = f", target={target!r}" if target is not None else ""
        if goal:
            return f"ctx.reach({goal!r}{success_text}{target_text})"
        return "ctx.reach(…)"
    if op == "commit":
        goal = payload.get("goal") or payload.get("task") or ""
        return f"ctx.commit({goal!r}, …)" if goal else "ctx.commit(…)"
    if op == "lookup":
        state = payload.get("state")
        entity = payload.get("entity") or "?"
        filters = payload.get("filters") or {}
        fields = payload.get("required_fields") or payload.get("fields") or []
        bits = [
            *([f"state={state}"] if state else []),
            f"entity={entity!r}",
        ]
        if filters:
            bits.append(f"filters={filters!r}")
        if fields:
            bits.append(f"fields={list(fields)[:6]!r}")
        return f"ctx.lookup({', '.join(bits)})"
    if op == "constrain":
        state = payload.get("state")
        entity = payload.get("entity") or "?"
        filters = payload.get("filters") or {}
        state_text = f"{state}, " if state else ""
        return f"ctx.constrain({state_text}{entity!r}, {filters!r})"
    if op == "acquire":
        state = payload.get("state")
        entity = payload.get("entity") or "?"
        fields = payload.get("fields") or []
        state_text = f"{state}, " if state else ""
        return (
            f"ctx.acquire({state_text}{entity!r}, "
            f"fields={list(fields)[:6]!r})"
        )
    if op == "read":
        fields = payload.get("fields") or []
        return f"ctx.read(fields={list(fields)[:8]!r})"
    if op == "focus":
        state = payload.get("state")
        fields = payload.get("fields") or []
        state_text = f"{state}, " if state else ""
        return f"ctx.focus({state_text}fields={list(fields)[:6]!r})"
    if op == "command":
        cap = payload.get("capability") or "?"
        return f"ctx.command({cap!r})"
    if op == "query":
        entity = payload.get("entity") or "?"
        return f"ctx.query({entity!r}, …)"
    if op == "gui_worker":
        worker_id = payload.get("worker_id") or payload.get("id") or "?"
        profile = payload.get("profile") or "operator"
        return f"ctx.gui_worker({worker_id!r}, profile={profile!r}, …)"
    if op == "data_worker":
        worker_id = payload.get("worker_id") or payload.get("id") or "?"
        return f"ctx.data_worker({worker_id!r}, …)"
    return f"ctx.{op}(…)"


_CODING_CTX_PLAN_OPS = frozenset({
    "reach", "commit", "gui", "write", "query", "lookup", "constrain", "focus",
    "acquire", "read", "command", "interact", "gui_worker", "data_worker",
})

# Plan-level ctx.* call → ordered runtime ops it may expand into.
_PLAN_RUNTIME_CONSUME: dict[str, tuple[str, ...]] = {
    "reach": ("reach",),
    "commit": ("commit",),
    "query": ("lookup", "constrain", "acquire"),
    "lookup": ("lookup",),
    "constrain": ("constrain",),
    "acquire": ("acquire",),
    "read": ("focus", "read"),
    "focus": ("focus",),
    "command": ("command",),
    "interact": ("reach", "commit"),
    "gui_worker": ("gui_worker",),
    "data_worker": ("data_worker",),
}


def _coding_plan_call_sites(source: str) -> list[dict]:
    """Static ctx.* call sites from source, source order (by lineno)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    sites: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "ctx"
            and func.attr in _CODING_CTX_PLAN_OPS
        ):
            continue
        sites.append({
            "op": {"gui": "reach", "write": "commit"}.get(
                func.attr, func.attr,
            ),
            "lineno": int(getattr(node, "lineno", 0) or 0),
            "end_lineno": int(
                getattr(node, "end_lineno", None)
                or getattr(node, "lineno", 0)
                or 0
            ),
        })
    sites.sort(key=lambda item: (item["lineno"], item["op"]))
    return sites


def _coding_runtime_calls(orchestrator: dict) -> list[dict]:
    """Normalized runtime statement list from report_run_log."""
    out: list[dict] = []
    for index, entry in enumerate(orchestrator.get("report_run_log") or [], 1):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        payload = (
            dict(entry["coding_payload"])
            if isinstance(entry.get("coding_payload"), dict)
            else {}
        )
        op = _infer_coding_op(
            coding_op=str(entry.get("coding_op") or ""),
            executor=str(entry.get("executor") or ""),
            inputs=payload,
        )
        if op == "reach" and not payload.get("goal"):
            payload = {**payload, "goal": name}
        plan = str(entry.get("coding_plan") or "")
        plan_step = int(entry.get("coding_plan_step") or 0)
        plan_steps = int(entry.get("coding_plan_steps") or 0)
        out.append({
            "ordinal": index,
            "sid": _coding_statement_id(entry),
            "call_id": str(entry.get("coding_call_id") or ""),
            "op": op,
            "phase": str(result.get("phase") or ""),
            "payload": payload,
            "name": name,
            "plan_op": plan,
            "plan_step": plan_step,
            "plan_steps": plan_steps,
        })
    return out


def _designed_plan_steps(plan_op: str, matched: list[dict]) -> int:
    """How many internal statements a plan-level API is designed to emit."""
    if plan_op == "query":
        recorded = max(
            (int(call.get("plan_steps") or 0) for call in matched),
            default=0,
        )
        if recorded:
            return recorded
        return 3
    if plan_op == "read":
        if any(str(c.get("op") or "") == "focus" for c in matched):
            return 2
        return 1
    return max(1, len(matched) or 1)


def _apply_plan_expansion_to_group(plan_op: str, matched: list[dict]) -> None:
    """Mutate matched runtime calls with plan_op / step / siblings for reporting."""
    if not matched:
        return
    designed = _designed_plan_steps(plan_op, matched)
    siblings = [int(c.get("ordinal") or 0) for c in matched]
    pending_ops: list[str] = []
    if plan_op == "query":
        expected = (
            ("lookup", "acquire")
            if designed == 2
            else ("lookup", "constrain", "acquire")
        )
        for pending_op in expected:
            if not any(c.get("op") == pending_op for c in matched):
                pending_ops.append(pending_op)
    if plan_op == "read" and designed == 2 and not any(c.get("op") == "read" for c in matched):
        pending_ops.append("read")
    for index, call in enumerate(matched, 1):
        # Prefer runtime-recorded plan meta when present.
        if not call.get("plan_op"):
            call["plan_op"] = plan_op
        if not call.get("plan_step"):
            call["plan_step"] = index
        if not call.get("plan_steps"):
            call["plan_steps"] = designed
        call["plan_siblings"] = siblings
        call["plan_pending"] = list(pending_ops)


def _match_runtime_to_plan_sites(
    plan_sites: list[dict],
    runtime_calls: list[dict],
) -> tuple[dict[int, list[dict]], list[dict]]:
    """Map plan-source linenos → runtime calls; leftover runtime calls returned separately."""
    by_line: dict[int, list[dict]] = {}
    queue = list(runtime_calls)
    for site in plan_sites:
        plan_op = str(site.get("op") or "")
        lineno = int(site.get("lineno") or 0)
        if not lineno or not queue:
            continue
        allowed = _PLAN_RUNTIME_CONSUME.get(plan_op, (plan_op,))
        matched: list[dict] = []
        call_id = str(queue[0].get("call_id") or "")
        if call_id and queue[0].get("op") in allowed:
            while queue and str(queue[0].get("call_id") or "") == call_id:
                matched.append(queue.pop(0))
            _apply_plan_expansion_to_group(plan_op, matched)
            by_line.setdefault(lineno, []).extend(matched)
            continue
        # Consume a contiguous prefix of allowed runtime ops (macro expansion).
        while queue and queue[0].get("op") in allowed:
            matched.append(queue.pop(0))
            # reach/commit/command: exactly one
            if plan_op in {"reach", "commit", "command", "lookup", "constrain", "acquire", "focus"}:
                break
            # query: stop after acquire if present, else keep taking its ordered phases
            if plan_op == "query" and matched[-1].get("op") == "acquire":
                break
            # read: stop after read
            if plan_op == "read" and matched[-1].get("op") == "read":
                break
        if matched:
            _apply_plan_expansion_to_group(plan_op, matched)
            by_line.setdefault(lineno, []).extend(matched)
    return by_line, queue


def _public_call_groups(calls: list[dict]) -> list[list[dict]]:
    """Contiguous private statements grouped by their public ctx.* invocation."""
    groups: list[list[dict]] = []
    keys: list[tuple] = []
    for call in calls:
        call_id = str(call.get("call_id") or "")
        siblings = tuple(call.get("plan_siblings") or ())
        key = (
            ("call", call_id)
            if call_id else
            ("siblings", siblings) if siblings else
            ("ordinal", int(call.get("ordinal") or 0))
        )
        if keys and key == keys[-1]:
            groups[-1].append(call)
        else:
            keys.append(key)
            groups.append([call])
    return groups


def _match_public_calls_to_plan_sites(
    plan_sites: list[dict],
    runtime_calls: list[dict],
) -> tuple[dict[int, list[dict]], list[dict]]:
    """Map dynamic public calls to source sites, reusing loop sites on back-edges."""
    by_line: dict[int, list[dict]] = {}
    leftovers: list[dict] = []
    cursor = 0
    for group in _public_call_groups(runtime_calls):
        op = str(group[0].get("plan_op") or group[0].get("op") or "")
        compatible = [
            index for index, site in enumerate(plan_sites)
            if str(site.get("op") or "") == op
        ]
        after_cursor = [index for index in compatible if index >= cursor]
        if after_cursor:
            site_index = after_cursor[0]
        elif compatible:
            site_index = compatible[-1]
        else:
            leftovers.extend(group)
            continue
        lineno = int(plan_sites[site_index].get("lineno") or 0)
        by_line.setdefault(lineno, []).extend(group)
        cursor = site_index + 1
    return by_line, leftovers


def _enrich_runtime_plan_expansion(
    runtime_calls: list[dict],
    *,
    source: str = "",
) -> dict[str, dict]:
    """Return sid → expansion meta; also mutates runtime_calls in place."""
    if not runtime_calls:
        return {}

    # New logs carry an explicit public ctx.* call id. It is the authoritative
    # grouping boundary for the private statements of one public ctx call.
    grouped_by_call: dict[str, list[dict]] = {}
    for call in runtime_calls:
        call_id = str(call.get("call_id") or "")
        if call_id:
            grouped_by_call.setdefault(call_id, []).append(call)
    for grouped in grouped_by_call.values():
        plan = str(grouped[0].get("plan_op") or grouped[0].get("op") or "reach")
        _apply_plan_expansion_to_group(plan, grouped)

    plan_sites = _coding_plan_call_sites(source) if source else []
    ungrouped = [
        call for call in runtime_calls if not str(call.get("call_id") or "")
    ]
    if plan_sites and ungrouped:
        _by_line, leftovers = _match_runtime_to_plan_sites(
            plan_sites, list(ungrouped),
        )
        for call in leftovers:
            plan = str(call.get("plan_op") or call.get("op") or "")
            if plan == "lookup" or call.get("op") == "lookup":
                plan = str(call.get("plan_op") or "query")
            _apply_plan_expansion_to_group(plan or "reach", [call])
    elif ungrouped:
        # No source: group by recorded plan tags, else query phases by order.
        i = 0
        while i < len(ungrouped):
            call = ungrouped[i]
            plan = str(call.get("plan_op") or "")
            if plan and int(call.get("plan_steps") or 0) > 1:
                group = [call]
                j = i + 1
                while (
                    j < len(ungrouped)
                    and str(ungrouped[j].get("plan_op") or "") == plan
                ):
                    group.append(ungrouped[j])
                    j += 1
                _apply_plan_expansion_to_group(plan, group)
                i = j
                continue
            if call.get("op") == "lookup":
                group = [call]
                j = i + 1
                while (
                    j < len(ungrouped)
                    and ungrouped[j].get("op") in {"constrain", "acquire"}
                ):
                    group.append(ungrouped[j])
                    if ungrouped[j].get("op") == "acquire":
                        j += 1
                        break
                    j += 1
                _apply_plan_expansion_to_group("query", group)
                i = j
                continue
            else:
                _apply_plan_expansion_to_group(
                    str(call.get("plan_op") or call.get("op") or "reach"), [call],
                )
            i += 1

    by_sid: dict[str, dict] = {}
    for call in runtime_calls:
        sid = str(call.get("sid") or "")
        if not sid:
            continue
        plan_op = str(call.get("plan_op") or call.get("op") or "")
        step = int(call.get("plan_step") or 1)
        steps = int(call.get("plan_steps") or 1)
        by_sid[sid] = {
            "plan_op": plan_op,
            "plan_step": step,
            "plan_steps": steps,
            "op": str(call.get("op") or ""),
            "ordinal": int(call.get("ordinal") or 0),
            "call_id": str(call.get("call_id") or ""),
            "siblings": list(call.get("plan_siblings") or [call.get("ordinal")]),
            "pending": list(call.get("plan_pending") or []),
            "expanded": bool(steps > 1 or plan_op in {"query", "read"} and plan_op != call.get("op")),
            "label": _plan_step_label(plan_op, str(call.get("op") or ""), step, steps),
        }
    return by_sid


def _plan_step_label(plan_op: str, step_op: str, step: int, steps: int) -> str:
    """Human label: ctx.query · 步骤 1/3 · lookup"""
    plan_op = plan_op or step_op or "?"
    step_op = step_op or "?"
    if steps > 1 or plan_op != step_op:
        return f"ctx.{plan_op} · 步骤 {step}/{steps} · {step_op}"
    return f"ctx.{plan_op}"


def _plan_is_expanded(plan_meta: dict | None, step_op: str = "") -> bool:
    if not isinstance(plan_meta, dict) or not plan_meta:
        return False
    plan_op = str(plan_meta.get("plan_op") or "")
    steps = int(plan_meta.get("plan_steps") or 0)
    op = step_op or str(plan_meta.get("op") or "")
    return bool(plan_op and (steps > 1 or (op and plan_op != op)))


def _macro_failure_verdict(
    plan_meta: dict | None,
    *,
    phase: str = "",
    step_op: str = "",
) -> str:
    """One-line diagnosis when a plan-level API dies mid-expansion."""
    if phase not in {"exhausted", "failed", "stopped", "interrupted"}:
        return ""
    if not _plan_is_expanded(plan_meta, step_op):
        return ""
    assert isinstance(plan_meta, dict)
    plan_op = str(plan_meta.get("plan_op") or "?")
    step = int(plan_meta.get("plan_step") or 1)
    steps = int(plan_meta.get("plan_steps") or 1)
    op = step_op or str(plan_meta.get("op") or "?")
    pending = [str(p) for p in (plan_meta.get("pending") or []) if p]
    text = f"ctx.{plan_op} 在步骤 {step}/{steps}（{op}）失败"
    if pending:
        text += "，" + "、".join(pending) + " 未执行"
    elif step < steps:
        text += f"，后续步骤未执行"
    return text


def _render_runtime_ann_chips(calls: list[dict]) -> str:
    chips: list[str] = []
    plan_op = ""
    plan_steps = 0
    if calls:
        plan_op = str(calls[0].get("plan_op") or "")
        plan_steps = int(calls[0].get("plan_steps") or 0)
    if plan_op and plan_steps > 1:
        chips.append(
            f'<span class="coding-src-chip coding-src-chip-plan">'
            f'<span class="coding-src-chip-op">ctx.{_safe(plan_op)}</span>'
            f'<span class="coding-src-chip-meta">{len(calls)}/{plan_steps} 步</span>'
            f'</span>'
        )
    for call in calls:
        ordinal = int(call.get("ordinal") or 0)
        sid = str(call.get("sid") or "")
        phase = str(call.get("phase") or "")
        phase_cls = {
            "completed": "coding-phase-ok",
            "exhausted": "coding-phase-fail",
            "failed": "coding-phase-fail",
        }.get(phase, "coding-phase-warn")
        phase_html = (
            f'<span class="coding-phase {phase_cls}">{_safe(phase)}</span>'
            if phase else ""
        )
        op = str(call.get("op") or "")
        step = int(call.get("plan_step") or 0)
        steps = int(call.get("plan_steps") or 0)
        step_meta = f"{step}/{steps}" if steps > 1 else ""
        title = _plan_step_label(
            str(call.get("plan_op") or ""), op, step or 1, steps or 1,
        )
        link = (
            f'<a class="coding-call-link" href="#ms-{_safe(sid)}" title="{_safe(title)}">'
            f'#{ordinal}</a>'
            if sid else f'<span class="coding-call-link">#{ordinal}</span>'
        )
        chips.append(
            f'<span class="coding-src-chip">{link}'
            f'<span class="coding-src-chip-op">{_safe(op or "?")}'
            f'{(" · " + step_meta) if step_meta else ""}</span>'
            f'{phase_html}</span>'
        )
    pending = list(calls[0].get("plan_pending") or []) if calls else []
    for pending_op in pending:
        chips.append(
            f'<span class="coding-src-chip coding-src-chip-pending">'
            f'<span class="coding-src-chip-op">{_safe(pending_op)} 未执行</span>'
            f'</span>'
        )
    return "".join(chips)


def _render_runtime_index_group(lineno: int, calls: list[dict]) -> str:
    invocations = _public_call_groups(calls)
    failed = any(
        call.get("phase") in {"exhausted", "failed", "stopped", "interrupted"}
        for call in calls
    )
    completed = sum(
        all(call.get("phase") == "completed" for call in invocation)
        for invocation in invocations
    )
    phase = "failed" if failed else "completed" if completed == len(invocations) else "running"
    phase_cls = (
        "coding-phase-fail" if failed
        else "coding-phase-ok" if phase == "completed"
        else "coding-phase-warn"
    )
    plan_op = str(calls[0].get("plan_op") or calls[0].get("op") or "?")
    meta = (
        f"×{len(invocations)} · {completed}/{len(invocations)} completed"
        if len(invocations) > 1
        else f"{len(calls)} 个内部阶段"
    )
    instances = []
    for index, invocation in enumerate(invocations, 1):
        label = (
            f'<span class="coding-src-instance-label">调用 {index}</span>'
            if len(invocations) > 1 else ""
        )
        instances.append(
            f'<div class="coding-src-instance">{label}'
            f'{_render_runtime_ann_chips(invocation)}</div>'
        )
    return (
        f'<details class="coding-src-api-group" id="coding-src-calls-{lineno}"'
        f'{" open" if failed else ""}>'
        f'<summary><a class="coding-src-index-line" href="#coding-src-line-{lineno}">'
        f'L{lineno}</a><code>ctx.{_safe(plan_op)}</code>'
        f'<span class="coding-src-api-meta">{meta}</span>'
        f'<span class="coding-phase {phase_cls}">{phase}</span></summary>'
        f'<div class="coding-src-api-body">{"".join(instances)}</div></details>'
    )


def _render_annotated_coding_source(source: str, orchestrator: dict) -> str:
    """Python source with runtime call annotations on matching ctx.* lines."""
    lines = source.splitlines()
    plan_sites = _coding_plan_call_sites(source)
    runtime_calls = _coding_runtime_calls(orchestrator)
    # Enrich plan expansion (query→lookup+constrain+acquire) before source alignment.
    _enrich_runtime_plan_expansion(runtime_calls, source=source)
    by_line, leftovers = _match_public_calls_to_plan_sites(
        plan_sites, list(runtime_calls),
    )

    # Fallback: no AST sites or no matches — keep chips in footer (don't mis-attach).
    if runtime_calls and not any(by_line.values()):
        leftovers = list(runtime_calls)
        by_line = {}

    row_html: list[str] = []
    index_html: list[str] = []
    ops_by_line: dict[int, list[str]] = {}
    for site in plan_sites:
        ops_by_line.setdefault(int(site.get("lineno") or 0), []).append(
            str(site.get("op") or "")
        )
    for lineno, raw in enumerate(lines, 1):
        code = _safe(raw) if raw else " "
        calls = by_line.get(lineno) or []
        has_ann = " coding-src-line-hit" if calls else ""
        for op in dict.fromkeys(ops_by_line.get(lineno, [])):
            token = f"ctx.{op}"
            token_class = "coding-src-api-token" + (
                " coding-src-api-token-run" if calls else ""
            )
            code = code.replace(
                token,
                f'<span class="{token_class}">{token}</span>',
            )
        if calls:
            index_html.append(_render_runtime_index_group(lineno, calls))
        row_html.append(
            f'<div class="coding-src-line{has_ann}" id="coding-src-line-{lineno}">'
            f'<span class="coding-src-ln">{lineno}</span>'
            f'<span class="coding-src-code">{code}</span>'
            f'</div>'
        )

    if leftovers:
        index_html.append(
            '<div class="coding-src-unmatched"><span>未对齐调用</span>'
            f'{_render_runtime_ann_chips(leftovers)}</div>'
        )
    has_failure = any(
        call.get("phase") in {"exhausted", "failed", "stopped", "interrupted"}
        for call in runtime_calls
    )
    call_index = (
        f'<details class="coding-src-index"{" open" if has_failure else ""}>'
        '<summary>运行调用索引'
        f'<span>{len(_public_call_groups(runtime_calls))} 个 ctx 调用'
        f'{" · 含失败，自动展开" if has_failure else " · 默认收起"}</span></summary>'
        f'<div class="coding-src-index-body">{"".join(index_html)}</div></details>'
        if index_html else ""
    )
    note = (
        '<div class="coding-src-legend">'
        '实色 ctx.API 与高亮行表示已执行；L 表示源码行，#N 表示 Statement 卡片'
        '</div>'
    )
    return (
        f'{note}'
        f'<div class="coding-source coding-source-annotated">'
        f'{"".join(row_html)}'
        f'</div>'
        f'{call_index}'
    )


def coding_plan_expansion_by_sid(orchestrator: dict | None) -> dict[str, dict]:
    """Public helper: statement id → plan expansion meta for card headers/data panels."""
    if not orchestrator:
        return {}
    source = str((orchestrator.get("program") or {}).get("source") or "")
    calls = _coding_runtime_calls(orchestrator)
    return _enrich_runtime_plan_expansion(calls, source=source)


def coding_source_line_by_call_id(orchestrator: dict | None) -> dict[str, int]:
    """Public ctx call id → source line, including repeated loop invocations."""
    if not orchestrator:
        return {}
    source = str((orchestrator.get("program") or {}).get("source") or "")
    calls = _coding_runtime_calls(orchestrator)
    _enrich_runtime_plan_expansion(calls, source=source)
    by_line, _ = _match_public_calls_to_plan_sites(
        _coding_plan_call_sites(source), calls,
    )
    return {
        str(call.get("call_id")): lineno
        for lineno, line_calls in by_line.items()
        for call in line_calls
        if call.get("call_id")
    }


def _render_compile_attempt_history(orchestrator: dict) -> str:
    attempts = [
        item for item in (orchestrator.get("compile_attempts") or [])
        if isinstance(item, dict)
    ]
    if not attempts:
        return ""
    failed = sum(not bool(item.get("passed")) for item in attempts)
    rows: list[str] = []
    for item in attempts:
        passed = bool(item.get("passed"))
        diagnostics = [str(value) for value in (item.get("diagnostics") or [])]
        usage = item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {}
        elapsed = float(item.get("elapsed_s") or 0)
        meta = [f"{elapsed:.1f}s"] if elapsed else []
        if usage:
            meta.append(
                f"{_fmt_tokens(int(usage.get('input') or 0))}/"
                f"{_fmt_tokens(int(usage.get('output') or 0))} tok"
            )
        verdict = "passed" if passed else f"{len(diagnostics)} issue(s)"
        phase_cls = "coding-phase-ok" if passed else "coding-phase-fail"
        detail = "\n".join(diagnostics) or "Program passed static review."
        rows.append(
            '<details class="coding-src-api-group"'
            + ("" if passed else " open")
            + '><summary>'
            f'<span class="coding-src-index-line">g{_safe(str(item.get("generation") or "?"))}.'
            f'a{_safe(str(item.get("attempt") or "?"))}</span>'
            f'<span class="coding-phase {phase_cls}">{_safe(verdict)}</span>'
            f'<span class="coding-src-api-meta">{_safe(" · ".join(meta))}</span>'
            '</summary><div class="coding-src-api-body">'
            f'<pre class="coding-data-pre">{_safe(detail)}</pre>'
            '</div></details>'
        )
    return (
        '<details class="coding-src-index">'
        f'<summary>Review history<span>{len(attempts)} attempts · {failed} repaired</span></summary>'
        f'<div class="coding-src-index-body">{"".join(rows)}</div>'
        '</details>'
    )


def _render_coding_program_shell(orchestrator: dict, program: dict) -> str:
    """Coding plan shell: goal, compile status, annotated Python source."""
    source = str(program.get("source") or "").strip()
    if not source:
        return ""
    goal = str(program.get("goal") or "")
    downstream_label = str(
        program.get("downstream_label") or "Statement 卡片（数据 + UI 交互）"
    )
    input_html = (
        f'<div class="prog-input"><span class="prog-input-label">输入</span>{_safe(goal)}'
        f'<span class="prog-input-arrow">↓ {_safe(downstream_label)}</span></div>'
        if goal else ""
    )
    compile_report = next(
        (
            report for report in reversed(orchestrator.get("context_reports") or [])
            if isinstance(report, dict)
            and report.get("kind") in {"coding_compile", "coding_review"}
        ),
        {},
    )
    if compile_report.get("kind") == "coding_compile":
        compile_label = (
            "Compile · 已重生成"
            if compile_report.get("repaired")
            else "Compile · 通过"
        )
    elif compile_report.get("degraded"):
        compile_label = "Review · 不可用"
    elif compile_report.get("repaired"):
        compile_label = "Review · 已重生成"
    elif compile_report.get("approved"):
        compile_label = "Review · 通过"
    elif compile_report:
        compile_label = "Review · 审计意见"
    else:
        compile_label = "Compile · 未记录"
    runtime_n = len(_coding_runtime_calls(orchestrator))
    compile_html = (
        '<div class="compat-row">'
        f'<span class="compat-chip">{_safe(compile_label)}</span>'
        f'<span class="coding-note">'
        f'源码高亮 {runtime_n} 次运行调用'
        f'</span>'
        '</div>'
    )
    source_html = (
        f'<details class="coding-source-wrap">'
        f'<summary>完整 Python 源码'
        f'<span class="coding-note"> · 高亮行对应下方运行调用索引</span></summary>'
        f'{_render_annotated_coding_source(source, orchestrator)}'
        f'</details>'
    )
    history_html = _render_compile_attempt_history(orchestrator)
    program_label = str(
        program.get("label") or "Coding Orchestrator · Python 执行计划"
    )
    return (
        '<div class="statement prog-section" id="ms-orchestrate-coding">'
        '<div class="statement-header">'
        '<h2>#0</h2>'
        f'<span class="statement-name">{_safe(program_label)}</span>'
        '<span class="statement-badge statement-badge-default">python</span>'
        f'{_render_orchestrator_metrics(orchestrator)}'
        '</div>'
        f'<div class="prog-body">{input_html}{compile_html}{history_html}{source_html}'
        f'{_render_orchestrator_context_reports(orchestrator)}'
        '</div>'
        '</div>'
    )


def _build_statement_call_params(
    *,
    name: str = "",
    success: str = "",
    executor: str = "",
    inputs: dict | None = None,
    coding_op: str = "",
    coding_payload: dict | None = None,
    call: dict | None = None,
) -> dict:
    """Assemble the full statement-executor call parameter object for the data panel."""
    call = dict(call) if isinstance(call, dict) else {}
    inputs = inputs if isinstance(inputs, dict) else {}
    coding_payload = dict(coding_payload) if isinstance(coding_payload, dict) else {}

    op = _infer_coding_op(
        coding_op=coding_op,
        executor=executor or str(call.get("executor") or ""),
        inputs=coding_payload or inputs or (
            call.get("inputs") if isinstance(call.get("inputs"), dict) else {}
        ),
    )
    flat = _flatten_coding_inputs(
        coding_op=op,
        coding_payload=coding_payload or None,
        inputs=inputs or (
            call.get("inputs") if isinstance(call.get("inputs"), dict) else {}
        ),
    )
    if op == "reach" and not flat.get("goal"):
        goal = name or str(call.get("goal") or "")
        if goal:
            flat = {**flat, "goal": goal}

    params: dict = {}
    if op:
        params["ctx_op"] = op
    # Prefer explicit coding payload when present (new runs); else flattened request.
    if coding_payload:
        params["ctx_payload"] = coding_payload
    elif flat:
        params["ctx_payload"] = flat

    # Full statement-executor contract (what the runtime actually dispatched).
    executor_call: dict = {}
    for key in (
        "id",
        "executor",
        "goal",
        "success",
        "on",
        "scope",
        "persistence",
        "inputs",
        "required_values",
        "observe_fields",
        "returns",
        "args",
        "capability",
        "required_fields",
        "reads",
        "coverage",
    ):
        if key in call and call.get(key) is not None:
            executor_call[key] = call[key]
    # Fill gaps from page-level fields when journal call snapshot is partial.
    if executor and "executor" not in executor_call:
        executor_call["executor"] = executor
    if name and "goal" not in executor_call:
        executor_call["goal"] = name
    if success and "success" not in executor_call:
        executor_call["success"] = success
    if inputs and "inputs" not in executor_call:
        executor_call["inputs"] = inputs
    if executor_call:
        params["statement"] = executor_call
    return params


def _coding_json_pre(value: object, *, limit: int = 8000) -> str:
    """Light-theme JSON/pre body for data panels (always the same surface)."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        text = text[: limit - 3] + "…"
    return f'<pre class="coding-data-pre">{_safe(text)}</pre>'


def _coding_arg_row(key: str, value: object) -> str:
    """One key/value row; scalars as value text, structures as light pre."""
    if value is None or isinstance(value, (bool, int, float)):
        body = f'<span class="coding-data-val">{_safe(str(value))}</span>'
    elif isinstance(value, str):
        body = f'<span class="coding-data-val">{_safe(value)}</span>'
    else:
        body = _coding_json_pre(value, limit=2400)
    return (
        f'<div class="coding-data-row">'
        f'<span class="coding-data-key">{_safe(key)}</span>'
        f'{body}'
        f'</div>'
    )


def _coding_details(title: str, body: str, *, plan: bool = False) -> str:
    """Render one consistently collapsed statement-data section."""
    block_class = " coding-data-block-plan" if plan else ""
    details_class = " coding-plan-details" if plan else ""
    return (
        f'<div class="coding-data-block{block_class}">'
        f'<details class="coding-data-details{details_class}">'
        f'<summary>{_safe(title)}</summary>'
        f'<div class="coding-data-details-body">{body}</div>'
        f'</details></div>'
    )


# Preferred field order for ctx payload rows — matches common call signatures.
_CTX_PAYLOAD_KEY_ORDER: dict[str, tuple[str, ...]] = {
    "reach": ("goal", "target"),
    "commit": ("goal", "target", "values"),
    "lookup": ("entity", "field", "fallback", "filters", "required_fields", "fields"),
    "constrain": ("entity", "filters"),
    "acquire": ("entity", "fields", "coverage", "scope"),
    "read": ("fields", "target"),
    "focus": ("target", "fields"),
    "command": ("capability",),
    "query": ("entity", "fields", "filters", "field", "fallback", "coverage"),
}


def _ordered_payload_items(op: str, payload: dict) -> list[tuple[str, object]]:
    """Stable, signature-first key order for readable call-arg rows."""
    preferred = _CTX_PAYLOAD_KEY_ORDER.get(op, ())
    seen: set[str] = set()
    items: list[tuple[str, object]] = []
    for key in preferred:
        if key in payload:
            items.append((key, payload[key]))
            seen.add(key)
    for key, value in payload.items():
        if key not in seen:
            items.append((str(key), value))
    return items


def _render_coding_data_panel(
    *,
    name: str = "",
    success: str = "",
    result: dict | None = None,
    outputs: dict | None = None,
    inputs: dict | None = None,
    coding_op: str = "",
    coding_payload: dict | None = None,
    executor: str = "",
    call: dict | None = None,
    plan_meta: dict | None = None,
    omit_call_label: bool = False,
) -> str:
    """Statement-local data: readable call args (light) + run result."""
    result = result if isinstance(result, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    if not outputs:
        raw_out = result.get("outputs")
        if isinstance(raw_out, dict):
            outputs = raw_out

    params = _build_statement_call_params(
        name=name,
        success=success,
        executor=executor,
        inputs=inputs,
        coding_op=coding_op,
        coding_payload=coding_payload,
        call=call,
    )
    op = str(params.get("ctx_op") or "")
    payload = params.get("ctx_payload") if isinstance(params.get("ctx_payload"), dict) else {}
    statement = params.get("statement") if isinstance(params.get("statement"), dict) else {}
    plan_meta = plan_meta if isinstance(plan_meta, dict) else {}

    sections: list[str] = []

    phase = str(result.get("phase") or "")
    plan_expanded = _plan_is_expanded(plan_meta, op)
    plan_op = str(plan_meta.get("plan_op") or "")
    plan_step = int(plan_meta.get("plan_step") or 0)
    plan_steps = int(plan_meta.get("plan_steps") or 0)

    # ── 0) 宏失败结论：抬到最前，避免先翻参数 ──
    verdict = _macro_failure_verdict(plan_meta, phase=phase, step_op=op)
    if verdict:
        sections.append(
            f'<div class="coding-macro-verdict">{_safe(verdict)}</div>'
        )

    # ── 1a) 计划展开：与其他数据区块一致，默认折叠 ──
    if plan_expanded:
        plan_rows: list[str] = [
            _coding_arg_row("API", f"ctx.{plan_op}"),
            _coding_arg_row("本步", f"{plan_step}/{plan_steps} · {op or '?'}"),
        ]
        siblings = [n for n in (plan_meta.get("siblings") or []) if n]
        if siblings:
            plan_rows.append(
                _coding_arg_row("关联卡片", " ".join(f"#{n}" for n in siblings)),
            )
        pending = plan_meta.get("pending") or []
        if pending:
            plan_rows.append(
                _coding_arg_row(
                    "未执行",
                    ", ".join(
                        f"ctx.{p}" if not str(p).startswith("ctx.") else str(p)
                        for p in pending
                    ),
                ),
            )
        summary = (
            f"计划展开 · ctx.{plan_op} 步骤 {plan_step}/{plan_steps}"
            if plan_op else "计划展开"
        )
        sections.append(_coding_details(summary, "".join(plan_rows), plan=True))

    # ── 1b) 本步参数：当前 Statement 内部 op 的入参（不再重复 op/签名）──
    step_rows: list[str] = []
    if op and not omit_call_label:
        step_rows.append(
            f'<div class="coding-data-row">'
            f'<span class="coding-data-key">调用</span>'
            f'<span class="coding-data-val"><code>{_safe(_coding_call_label(op, payload))}</code></span>'
            f'</div>'
        )
    for field, value in _ordered_payload_items(op, payload)[:24]:
        if value is None:
            continue
        # Skip empty optional strings that only clutter the form (e.g. fallback="").
        if value == "" or value == [] or value == {}:
            continue
        step_rows.append(_coding_arg_row(str(field), value))
    if step_rows:
        step_title = "本步参数" if plan_expanded else "调用参数"
        sections.append(_coding_details(step_title, "".join(step_rows)))

    # ── 2) 运行结果（failure 与顶部宏结论重复时，只保留 evidence 等补充信息）──
    result_rows: list[str] = []
    if phase and not verdict:
        result_rows.append(_coding_arg_row("phase", phase))
    summary = str(result.get("summary") or "")
    failure = str(result.get("failure_evidence") or "")
    # When macro verdict already names the failure locus, skip duplicating the same text.
    if summary and (not verdict or summary.strip() not in verdict):
        # Also skip if summary == failure and we'll show failure once.
        if not (failure and summary.strip() == failure.strip() and verdict):
            result_rows.append(_coding_arg_row("summary", summary))
    if failure and not verdict:
        result_rows.append(
            f'<div class="coding-data-row coding-data-fail">'
            f'<span class="coding-data-key">failure</span>'
            f'<span class="coding-data-val">{_safe(failure)}</span></div>'
        )
    elif failure and verdict and failure.strip() not in verdict and (
        not summary or failure.strip() != summary.strip()
    ):
        result_rows.append(
            f'<div class="coding-data-row coding-data-fail">'
            f'<span class="coding-data-key">detail</span>'
            f'<span class="coding-data-val">{_safe(failure)}</span></div>'
        )
    evidence = result.get("evidence") or []
    if isinstance(evidence, list) and evidence:
        for index, item in enumerate(evidence[:6], 1):
            if not item:
                continue
            result_rows.append(_coding_arg_row(f"ev.{index}", str(item)))
    if outputs:
        if len(json.dumps(outputs, ensure_ascii=False, default=str)) > 6000:
            out_body = _report_value(outputs, sample_items=3, limit=2400)
            result_rows.append(
                f'<div class="coding-data-row">'
                f'<span class="coding-data-key">outputs</span>'
                f'{_coding_json_pre(out_body)}'
                f'</div>'
            )
        else:
            result_rows.append(
                f'<div class="coding-data-row">'
                f'<span class="coding-data-key">outputs</span>'
                f'{_coding_json_pre(outputs)}'
                f'</div>'
            )
    if result_rows:
        sections.append(_coding_details("运行结果", "".join(result_rows)))

    # ── 3) Statement 契约（低频细节，放最后）──
    if statement:
        sections.append(_coding_details(
            "Statement 执行器契约",
            _coding_json_pre(statement),
        ))

    if not sections:
        if name:
            return (
                f'<div class="coding-stmt-data">'
                f'{_coding_details("数据", _coding_arg_row("goal", name))}'
                f'</div>'
            )
        return ""
    return f'<div class="coding-stmt-data">{"".join(sections)}</div>'
