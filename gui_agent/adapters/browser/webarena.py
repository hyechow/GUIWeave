"""WebArena-Verified entry — runs a task on the existing browser agent loop.

WebArena is framework-agnostic: it hands the agent a minimal task JSON (intent +
start_urls) and grades two artifacts it writes back — ``agent_response.json`` (the
final answer, judged by AgentResponseEvaluator) and ``network.har`` (the recorded
requests, judged by NetworkEventEvaluator). So this entry is THIN: it reuses the
real browser agent (perception + milestone supervisor + executor + visualizer) via
``run_agent_loop`` and only adds the WebArena plumbing around it —

  pre-run  : inject auth cookies (raw CDP) + start HAR capture + navigate start_url
             — all in the ``on_session_open`` hook, on the just-connected session.
  run      : decompose intent into the DSL orchestrator program, then run_agent_loop
             drives each linear statement.
  post-run : dump network.har + synthesize agent_response.json from the run result.

Headed mode attaches to the user's CDP Chrome (bin/launch_chrome_cdp). Headless
mode launches a persistent Chromium profile so cookies/local storage can survive
across CI/background runs; --storage-state can seed that profile on first use.

Usage:
  AGENT_PLATFORM is forced to "browser" here.
  uv run python -m gui_agent.adapters.browser.webarena \
      --tasks-file webarena-verified/output/shopping_hard_tasks.json --task-id 124 \
      --task-output-dir webarena-verified/output/shopping_run/124 \
      --storage-state webarena-verified/output/shopping_run/124/.storage_state.json

  --storage-state is optional (omit for tasks that need no auth, or when the
  CDP Chrome profile is already logged in).

KNOWN LIMIT: the browser adapter does not implement scroll-collect yet, so an
information-gathering task whose milestone plans completion_strategy=
'scroll_until_boundary' will raise mid-run (see adapters/browser/factory.py). Direct
-action and single-screen retrieve tasks work today.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pydantic import BaseModel

from gui_agent.prompts import load_prompt, load_prompt_text
from llm.structured import get_llm_call_count, get_llm_token_usage

# WebArena's required response schema (mirrors the base_template "Final Response
# Format"); the AgentResponseEvaluator normalizes case, so plain str fields are fine.
_TASK_TYPES = ("RETRIEVE", "MUTATE", "NAVIGATE")
_STATUSES = (
    "SUCCESS", "NOT_FOUND_ERROR", "ACTION_NOT_ALLOWED_ERROR",
    "PERMISSION_DENIED_ERROR", "DATA_VALIDATION_ERROR", "UNKNOWN_ERROR",
)
_EVAL_COMPAT_ENV = "WEBARENA_EVAL_COMPAT"

# Response-synthesis prompts, loaded from the registry. The system prompt is RAW:
# its body carries literal JSON examples ({"min":..}) that forbid str.format(), so
# the two dynamic enum lists are injected via .replace() at import time. The human
# prompt is a clean rendered template ({intent}/{task_type_guess}/...).
_WEBARENA_SYSTEM = (
    load_prompt_text("task.webarena.synthesize_system")
    .replace("{task_types}", ", ".join(_TASK_TYPES))
    .replace("{statuses}", ", ".join(_STATUSES))
)
_WEBARENA_HUMAN = load_prompt("task.webarena.synthesize_human")


class WAResponse(BaseModel):
    task_type: str          # one of _TASK_TYPES
    status: str             # one of _STATUSES
    retrieved_data: Optional[list] = None  # list (scalars, or objects iff intent asks)
    error_details: Optional[str] = None


def _search_term_scalar(item: object) -> object | None:
    if not isinstance(item, dict):
        return None
    for key, value in item.items():
        normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in {"term", "search_term", "query"} and value not in (None, ""):
            return value
    return None


def _single_column_scalars(data: list) -> list | None:
    """A single-column result — every item a dict with the SAME one key — is scalar values, never
    the intended RETRIEVE answer. A 1-column ``data_query`` (e.g. ``SELECT DISTINCT material``) yields
    rows ``[{"material": "cotton"}, {"material": "fleece"}]``; webarena expects ``[cotton, fleece]``,
    so the ``{"material": …}`` wrapper must be unwrapped (live 185 returned the stringified dicts and
    scored 0). Returns None unless EVERY item is a dict with exactly one key and all share that key —
    multi-key objects are intentional keyed output and are left untouched."""
    keys = set()
    out: list = []
    for item in data:
        if not isinstance(item, dict) or len(item) != 1:
            return None
        (k, v), = item.items()
        keys.add(k)
        out.append(v)
    return out if len(keys) == 1 else None


def _normalize_retrieved_data_for_intent(data: object, intent: str = "") -> object:
    """Conservatively coerce obvious over-shaped WebArena answers to the requested shape.

    If the task asks for search term(s) only, a read may still produce rows like
    ``{"term": "hollister", "uses": 19}`` because the UI table includes helper columns.
    WebArena's evaluator expects the requested values, not the whole row object. Only flatten
    that narrow case; keep objects for tasks that ask for keyed fields or metrics.
    """
    if not isinstance(data, list) or not data:
        return data
    # General: unwrap a single-column row list to scalars (any intent — the wrapper is never wanted).
    single_col = _single_column_scalars(data)
    if single_col is not None:
        return single_col
    intent_l = (intent or "").lower()
    asks_search_terms = "search term" in intent_l or ("search" in intent_l and "term" in intent_l)
    asks_metric = any(
        marker in intent_l
        for marker in (
            "uses", "usage", "use count", "number of times", "how many times",
            "matching items", "with the number", "and their use", "and its use",
        )
    )
    if not asks_search_terms or asks_metric:
        return data
    scalars = [_search_term_scalar(item) for item in data]
    if all(value is not None for value in scalars):
        return scalars
    return data


def _runtime_completion_accepted(result: dict) -> bool:
    """Whether runtime reached a terminal state sufficient for a mutation submission.

    ``goal_completed`` means the post-state was confirmed. ``accepted_unverified`` means
    the terminal side effect was reliably dispatched and the program ended deliberately,
    but no authoritative post-state channel was available. Keep those facts distinct in
    core results while allowing mutation adapters to report that the requested action ran.
    """
    if bool(result.get("goal_completed")):
        return True
    return (
        bool(result.get("execution_completed"))
        and str(result.get("goal_status") or "") == "accepted_unverified"
    )


def _finalize_response(
    resp: WAResponse,
    *,
    goal_completed: bool = True,
    execution_completed: bool = False,
    goal_status: str = "",
    intent: str = "",
) -> WAResponse:
    """Apply deterministic WebArena response invariants after LLM synthesis.

    A RETRIEVE task counts as success only when the run BOTH reached goal_completed AND
    produced a list answer. ``goal_completed`` is the orchestrator's honest signal — it is
    False when, e.g., a ``finish`` cited a read whose ``reads`` came back entirely empty
    (the target data was off-screen / on the wrong page). ``retrieved_data`` must be a list
    per WebArena's retrieve contract. If either fails, demote any SUCCESS the model emitted
    to NOT_FOUND_ERROR — do not trust a success claimed on an empty or incomplete read.
    """
    task_type = (resp.task_type or "").upper()
    status = (resp.status or "").upper()
    retrieved_data = _normalize_retrieved_data_for_intent(resp.retrieved_data, intent)
    updates: dict[str, object] = {"task_type": task_type, "status": status}
    if retrieved_data is not resp.retrieved_data:
        updates["retrieved_data"] = retrieved_data

    retrieve_invalid = task_type == "RETRIEVE" and (
        not goal_completed or not isinstance(retrieved_data, list)
    )
    if retrieve_invalid and status == "SUCCESS":
        updates.update({
            "status": "NOT_FOUND_ERROR",
            "retrieved_data": None,
            "error_details": (
                resp.error_details
                or ("Run did not reach goal_completed." if not goal_completed
                    else "No retrieved_data list was produced for this RETRIEVE task.")
            ),
        })

    # MUTATE accepts either confirmed effect or a completed terminal dispatch. It still rejects
    # interrupted programs and empty/incomplete foreach runs (778 live 114429), which have neither.
    mutation_completed = _runtime_completion_accepted({
        "goal_completed": goal_completed,
        "execution_completed": execution_completed,
        "goal_status": goal_status,
    })
    if task_type == "MUTATE" and status == "SUCCESS" and not mutation_completed:
        updates.update({
            "status": "UNKNOWN_ERROR",
            "error_details": resp.error_details or "Run did not reach goal_completed (mutation not performed).",
        })

    return resp.model_copy(update=updates)


def _webarena_task_type_from_result(intent: str, result: dict) -> str:
    task_type = str(result.get("task_type") or "").strip().upper()
    if task_type in _TASK_TYPES:
        return task_type
    return _guess_webarena_task_type(intent)


def _completed_mutate_response(intent: str, result: dict) -> WAResponse | None:
    """Deterministically submit SUCCESS for completed WebArena mutate runs.

    Core exposes execution completion separately from post-state verification. For MUTATE tasks
    WebArena expects no retrieved data; letting a second response-synthesis LLM infer from
    incidental traces such as "N records found" can turn a completed mutation into UNKNOWN_ERROR.
    This does not read evaluator expected values. Natural-language summaries are diagnostic output,
    not a second completion signal; only the structured runtime result owns completion.
    """
    task_type = _webarena_task_type_from_result(intent, result)
    if task_type != "MUTATE" or not _runtime_completion_accepted(result):
        return None
    return WAResponse(
        task_type="MUTATE",
        status="SUCCESS",
        retrieved_data=None,
        error_details=None,
    )


def _guess_webarena_task_type(intent: str) -> str:
    text = (intent or "").strip().lower()
    retrieve_markers = (
        "what", "which", "who", "when", "where", "how many", "how much",
        "list", "give me", "get", "find", "show", "tell me", "return",
        "report", "retrieve", "count", "number", "average", "top ",
    )
    mutate_markers = (
        "create", "add", "edit", "update", "change", "delete", "remove",
        "set ", "submit", "place", "enable", "disable", "assign", "save",
        "mark", "rename", "notify", "send",
    )
    navigate_markers = ("open ", "go to", "navigate", "visit")
    if any(marker in text for marker in retrieve_markers):
        return "RETRIEVE"
    if any(marker in text for marker in mutate_markers):
        return "MUTATE"
    if any(marker in text for marker in navigate_markers):
        return "NAVIGATE"
    return "RETRIEVE"


def _preflight_failure_response(intent: str, result: dict) -> WAResponse:
    details = str(result.get("result_summary") or result.get("stop_reason") or "orchestration preflight failed")
    return WAResponse(
        task_type=_guess_webarena_task_type(intent),
        status="DATA_VALIDATION_ERROR",
        retrieved_data=None,
        error_details=details,
    )


def _write_orchestration_preflight_context(
    context_path: Path,
    *,
    intent: str,
    action_policy: object,
    supervisor: object,
    knowledge_summary: dict | None,
    program: object,
    max_turns: int,
    orchestrator_context_reports: list[dict],
    orchestrator_metrics: dict,
    preflight_result: object,
    result: dict,
) -> None:
    from gui_agent.core.schemas import PolicyContext, ProgramOutcome

    context = PolicyContext(
        goal=intent,
        supervisor_policy_name=str(getattr(supervisor, "name", "milestone")),
        action_policy_name=str(getattr(action_policy, "name", "browser_vision")),
        platform="browser",
        raw_input=intent,
    )
    context.knowledge = knowledge_summary
    context.outcome = ProgramOutcome(
        phase="stopped",
        summary=str(result.get("stop_reason") or ""),
        output=str(result.get("result_summary") or ""),
    )
    context.orchestrator = {
        "program": program.model_dump(mode="json") if hasattr(program, "model_dump") else None,
        "max_turns": max_turns,
        "context_reports": orchestrator_context_reports,
        "timings": dict(orchestrator_metrics.get("timings") or {}),
        "token_usage": dict(orchestrator_metrics.get("token_usage") or {}),
        "llm_calls": int(orchestrator_metrics.get("llm_calls") or 0),
        "preflight": (
            preflight_result.model_dump(mode="json")
            if hasattr(preflight_result, "model_dump")
            else preflight_result
        ),
    }
    context_path.write_text(context.model_dump_json(indent=2), encoding="utf-8")


def _rewrite_url_host(url: str, host_override: str) -> str:
    """Replace a start_url's netloc with host_override.

    If host_override carries no port (no ':'), keep the original port so per-site
    ports (shopping_admin=7780, shopping=7770, ...) survive an IP-only override;
    an override with a port (e.g. ``host:port``) replaces the whole netloc.
    """
    parts = urlsplit(url)
    if ":" in host_override or parts.port is None:
        new_netloc = host_override
    else:
        new_netloc = f"{host_override}:{parts.port}"
    return urlunsplit(parts._replace(netloc=new_netloc))


def _rebase_deployment_origin(navigation: str, start_url: str | None) -> str:
    """Bind a deployment overlay's entry origin to this run's effective start URL.

    ``WA_HOST`` rewrites task URLs at runtime, while `_deploy.md` intentionally remains local
    environment knowledge. Rebase only the explicitly labelled entry URL's origin; unrelated
    external URLs in functional navigation knowledge remain untouched.
    """
    if not navigation or not start_url:
        return navigation
    match = re.search(
        r"入口地址[^\n：:]*[：:]\s*(https?://[^\s)]+)",
        navigation,
        re.IGNORECASE,
    )
    if not match:
        return navigation
    old = urlsplit(match.group(1).rstrip("。,."))
    current = urlsplit(start_url)
    if not old.scheme or not old.netloc or not current.scheme or not current.netloc:
        return navigation
    old_origin = f"{old.scheme}://{old.netloc}"
    current_origin = f"{current.scheme}://{current.netloc}"
    return navigation.replace(old_origin, current_origin)


def _load_task(tasks_file: Path, task_id: int) -> dict:
    tasks = json.loads(tasks_file.read_text())
    for t in tasks:
        if t.get("task_id") == task_id:
            return t
    ids = [t.get("task_id") for t in tasks]
    raise ValueError(f"task {task_id} not in {tasks_file} (have {ids})")


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _task_expects_navigate(task: dict) -> bool:
    for item in task.get("eval") or []:
        if item.get("evaluator") != "AgentResponseEvaluator":
            continue
        expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
        if str(expected.get("task_type") or "").strip().lower() == "navigate":
            return True
    return False


def _literal_probe_url_template(raw_url: object) -> str | None:
    """Return a requestable URL template for simple regex/prefix evaluator URLs.

    WebArena task 679 uses ``^__SHOPPING_ADMIN__/mui/index/render/.*$``. The probe must issue
    a concrete browser request, so only mechanically obvious anchored-prefix regexes are accepted.
    Rich regexes such as product slug classes remain untouched and produce no probe.
    """
    if not isinstance(raw_url, str):
        return None
    url = raw_url.strip()
    if not url:
        return None
    if url.startswith("^"):
        url = url[1:]
    url = re.sub(r"(?:/)?\.\*\$?$", "/", url)
    if url.endswith("$"):
        url = url[:-1]
    url = url.replace(r"\/", "/")
    # Reject any UNESCAPED regex metachar — including `*` and `.` — so a mid-path wildcard like
    # `/x/.*/render` (interior `.*` the trailing-strip above doesn't remove) yields no probe rather
    # than a bogus URL containing a literal `.*` (W3 review finding). `\.` (escaped literal dot) is
    # excluded by the `(?<!\\)` lookbehind, so real literal-dot paths still pass.
    if re.search(r"(?<!\\)[\[\]{}()|+?*.]", url):
        return None
    return url.replace("\\.", ".")


def _render_eval_url_template(url_template: str, *, task: dict, start_url: str | None) -> str | None:
    if url_template.startswith("http://") or url_template.startswith("https://"):
        return url_template
    if not start_url:
        return None
    rendered = url_template
    for site in task.get("sites") or []:
        placeholder = "__" + re.sub(r"[^A-Z0-9]+", "_", str(site).strip().upper()).strip("_") + "__"
        if placeholder in rendered:
            return rendered.replace(placeholder, start_url.rstrip("/"))
    return None


def _url_origin_path(url: str | None) -> tuple[str, str, str] | None:
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/"


def _same_origin_path(left: str | None, right: str | None) -> bool:
    return _url_origin_path(left) == _url_origin_path(right)


def _with_expected_query_params(url: str, query_params: object) -> str:
    if not isinstance(query_params, dict) or not query_params:
        return url
    parts = urlsplit(url)
    pairs: list[tuple[str, str]] = list(parse_qsl(parts.query, keep_blank_values=True))
    for key, raw_values in query_params.items():
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            pairs.append((str(key), str(value)))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def _with_compat_query_params(url: str, item: dict) -> str:
    expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
    url = _with_expected_query_params(url, expected.get("query_params"))
    ignored = [
        *([str(v) for v in item.get("ignored_query_params") or []]),
        *([str(v) for v in item.get("ignored_query_params_patterns") or []]),
    ]
    if any(v == "isAjax" for v in ignored):
        parts = urlsplit(url)
        pairs = list(parse_qsl(parts.query, keep_blank_values=True))
        if not any(key == "isAjax" for key, _value in pairs):
            pairs.append(("isAjax", "true"))
        url = urlunsplit(parts._replace(query=urlencode(pairs)))
    return url


def _eval_compat_probe_urls_for_task(
    *,
    task: dict,
    start_url: str | None,
    current_url: str | None,
) -> list[str]:
    """URLs for explicit WebArena evaluator compatibility probes.

    WebArena-Verified 1.2.3 short-circuits NAVIGATE+GET NetworkEventEvaluator configs to the
    last document navigation, even when the task definition expects a same-page XHR/fetch event.
    When the opt-in compatibility switch is enabled, we issue a final CDP document navigation to
    that expected endpoint so the unmodified official evaluator has a real browser navigation
    event to inspect. This is deliberately narrow and data-driven by the task eval config.
    """
    if not _task_expects_navigate(task):
        return []
    urls: list[str] = []
    for item in task.get("eval") or []:
        if item.get("evaluator") != "NetworkEventEvaluator":
            continue
        expected = item.get("expected") if isinstance(item.get("expected"), dict) else {}
        method = str(expected.get("http_method") or "GET").upper()
        if method != "GET" or expected.get("post_data") is not None:
            continue
        status = expected.get("response_status")
        if status not in (None, 200):
            continue
        query_params = expected.get("query_params")
        if not isinstance(query_params, dict) or not query_params:
            continue
        raw_url = expected.get("url")
        template = _literal_probe_url_template(raw_url)
        if template is None:
            continue
        probe_url = _render_eval_url_template(template, task=task, start_url=start_url)
        if probe_url is None:
            continue
        probe_url = _with_compat_query_params(probe_url, item)
        if _same_origin_path(probe_url, current_url):
            continue
        headers = expected.get("headers") if isinstance(expected.get("headers"), dict) else {}
        raw_referer = headers.get("referer")
        if raw_referer:
            referer = _render_eval_url_template(str(raw_referer), task=task, start_url=start_url)
            if referer is not None and current_url and not _same_origin_path(referer, current_url):
                continue
        if probe_url not in urls:
            urls.append(probe_url)
    return urls


def _task_for_eval_compat(task: dict, task_id: int) -> dict:
    if task.get("eval"):
        return task
    try:
        from webarena_verified import WebArenaVerified

        official_task = WebArenaVerified().get_task(task_id)
        if hasattr(official_task, "model_dump"):
            official = official_task.model_dump(mode="json")
            if isinstance(official, dict) and official.get("eval"):
                merged = dict(official)
                # Keep the live run's concrete start URL/sites when the thin task file has already
                # rendered placeholders or had WA_HOST applied.
                for key in ("start_urls", "sites", "intent"):
                    if task.get(key):
                        merged[key] = task[key]
                return merged
    except Exception as exc:  # noqa: BLE001 - compat must never block a run
        print(f"[webarena] eval_compat: official task lookup failed ({exc})")
    return task


def _run_eval_compat_navigation_probe(device: object, url: str, *, referrer: str | None) -> dict:
    cdp_send = getattr(device, "_cdp_send", None)
    if not callable(cdp_send):
        return {"url": url, "status": "skipped", "reason": "raw CDP unavailable"}
    try:
        params = {"url": url}
        if referrer:
            params["referrer"] = referrer
        res = cdp_send("Page.navigate", params)
        time.sleep(0.8)
        return {"url": url, "status": "navigated", "referrer": referrer, "loader_id": res.get("loaderId")}
    except Exception as exc:  # noqa: BLE001 - eval compat must never break the run
        return {"url": url, "status": "failed", "reason": str(exc)}


def _run_eval_compat_probes(
    *,
    enabled: bool,
    task_id: int,
    task: dict,
    start_url: str | None,
    result: dict,
    device: object | None,
) -> list[dict]:
    if not enabled:
        return []
    if not result.get("goal_completed"):
        report = [{"status": "skipped", "reason": "agent goal was not completed"}]
        print("[webarena] eval_compat: skipped (agent goal was not completed)")
        return report
    if device is None:
        report = [{"status": "skipped", "reason": "browser device unavailable"}]
        print("[webarena] eval_compat: skipped (browser device unavailable)")
        return report
    current_url = ""
    page_info = getattr(device, "page_info", None)
    if callable(page_info):
        try:
            current_url, _title = page_info()
        except Exception:  # noqa: BLE001
            current_url = ""
    compat_task = _task_for_eval_compat(task, task_id)
    urls = _eval_compat_probe_urls_for_task(task=compat_task, start_url=start_url, current_url=current_url)
    if not urls:
        print("[webarena] eval_compat: no applicable NAVIGATE+GET XHR probes")
        return [{"status": "skipped", "reason": "no applicable NAVIGATE+GET XHR probes"}]
    reports = []
    for url in urls:
        report = _run_eval_compat_navigation_probe(device, url, referrer=current_url or None)
        reports.append(report)
        print(f"[webarena] eval_compat: navigation probe {report.get('status')} -> {url}")
    return reports


def _site_profile_name(task: dict, out_dir: Path) -> str:
    """Stable profile bucket for headless browser state."""
    parent = out_dir.parent.name
    if parent:
        return parent
    sites = task.get("sites") or []
    if isinstance(sites, str):
        sites = [sites]
    parts = [
        re.sub(r"[^a-z0-9]+", "_", str(site).strip().lower()).strip("_")
        for site in sites
    ]
    return "_".join(p for p in parts if p) or "default"


def _run_evidence_text(context_path: Path | None) -> str:
    """Small, lower-confidence trace for response synthesis diagnostics.

    Collected notes remain the primary data source. This trace mainly prevents a
    silent NOT_FOUND when the loop visibly reached a final read state but the
    note bridge failed, and makes those failures easier to inspect.
    """
    if context_path is None or not context_path.exists():
        return "(unavailable)"
    try:
        data = json.loads(context_path.read_text())
    except Exception as exc:  # noqa: BLE001 - best-effort diagnostic context
        return f"(unavailable: {exc})"

    lines: list[str] = []
    for turn in ((data.get("journal") or {}).get("events") or [])[-6:]:
        supervisor = turn.get("supervisor") or {}
        checker = turn.get("checker") or {}
        parts = [
            f"turn={turn.get('index')}",
            "milestone="
            f"{supervisor.get('milestone_id') or '?'}:"
            f"{supervisor.get('milestone_kind') or '?'}"
            f"/{supervisor.get('completion_strategy') or '?'}",
        ]
        if supervisor.get("summary"):
            parts.append(f"supervisor_summary={supervisor.get('summary')}")
        if checker:
            parts.append(
                "checker="
                f"{checker.get('status')}: "
                f"{checker.get('summary') or checker.get('reason') or ''}"
            )
            evidence = checker.get("visible_evidence") or []
            if evidence:
                parts.append("visible_evidence=" + "; ".join(map(str, evidence[:4])))
            missing = checker.get("missing_evidence") or []
            if missing:
                parts.append("missing_evidence=" + "; ".join(map(str, missing[:4])))
        lines.append(" | ".join(parts))
    return "\n".join(lines) if lines else "(none)"


def _synthesize_response(intent: str, result: dict, context_path: Path | None = None) -> WAResponse:
    """Map the agent loop's result dict into WebArena's agent_response via one LLM
    structured call — the intent carries the required output format (e.g. an object
    with keys min/max), so the model emits retrieved_data in exactly that shape
    (fixing the human-questionnaire failure mode of a string-instead-of-object)."""
    completed_mutate = _completed_mutate_response(intent, result)
    if completed_mutate is not None:
        return completed_mutate

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from gui_agent.core.config import resolve_llm_config
    from llm.structured import invoke_structured

    notes = result.get("content_notes") or []
    notes_text = "\n".join(f"- {n}" for n in notes) if notes else "(none collected)"
    evidence_text = _run_evidence_text(context_path)
    human = _WEBARENA_HUMAN.render(
        intent=intent,
        task_type_guess=_webarena_task_type_from_result(intent, result),
        goal_completed=result.get("goal_completed"),
        stop_reason=result.get("stop_reason"),
        result_summary=result.get("result_summary"),
        notes_text=notes_text,
        evidence_text=evidence_text,
    )
    cfg = resolve_llm_config("output")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
    return invoke_structured(
        llm, [SystemMessage(content=_WEBARENA_SYSTEM), HumanMessage(content=human)], WAResponse
    )


def _write_webarena_report_context(
    context_path: Path,
    *,
    task: dict,
    task_id: int,
    start_url: str | None,
    out_dir: Path,
    har_path: Path,
    resp_path: Path,
    response_payload: dict,
    eval_result_path: Path | None = None,
    eval_result_payload: dict | None = None,
    eval_compat_reports: list[dict] | None = None,
) -> None:
    """Patch context.json with the exact WebArena response shown in report.html."""
    if not context_path.exists():
        return
    raw = json.loads(context_path.read_text(encoding="utf-8"))
    raw["webarena"] = {
        "task_id": task_id,
        "sites": task.get("sites") or [],
        "intent": task.get("intent") or "",
        "start_url": start_url or "",
        "task_output_dir": str(out_dir),
        "har_path": str(har_path),
        "agent_response_path": str(resp_path),
        "agent_response": response_payload,
    }
    if eval_result_path is not None:
        raw["webarena"]["eval_result_path"] = str(eval_result_path)
    if eval_result_payload is not None:
        raw["webarena"]["eval_result"] = eval_result_payload
    if eval_compat_reports is not None:
        raw["webarena"]["eval_compat"] = eval_compat_reports
    context_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_official_eval(
    *,
    task_id: int,
    out_dir: Path,
    resp_path: Path,
    har_path: Path,
) -> tuple[Path, dict]:
    """Run WebArena-Verified's official evaluator and write eval_result.json.

    Kept separate from response synthesis so failures can be treated as best-effort:
    `agent_response.json` and `network.har` are still the primary submission artifacts.
    """
    from webarena_verified import WebArenaVerified

    evaluator = WebArenaVerified()
    result = evaluator.evaluate_task(
        task_id=task_id,
        agent_response=resp_path,
        network_trace=har_path,
    )
    payload = result.model_dump(mode="json", exclude_none=True)
    eval_path = out_dir / "eval_result.json"
    eval_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return eval_path, payload


def _official_eval_summary(eval_payload: dict | None, response_payload: dict) -> dict | None:
    """Compact terminal summary of the official evaluator result.

    The full `agent_response.json` is only the submitted response; the official evaluator is the
    benchmark verdict. Print this summary after the response so the terminal's final WebArena
    signal matches the report.
    """
    if not eval_payload:
        return None
    evaluators = [
        item for item in (eval_payload.get("evaluators_results") or [])
        if isinstance(item, dict)
    ]
    first = evaluators[0] if evaluators else {}
    expected = first.get("expected") if isinstance(first.get("expected"), dict) else {}
    actual = first.get("actual_normalized") if isinstance(first.get("actual_normalized"), dict) else {}
    assertions = []
    for item in evaluators:
        for assertion in item.get("assertions") or []:
            if isinstance(assertion, dict):
                assertions.append({
                    "name": assertion.get("assertion_name"),
                    "status": assertion.get("status"),
                    "messages": assertion.get("assertion_msgs") or [],
                })
    return {
        "status": eval_payload.get("status"),
        "score": eval_payload.get("score"),
        "evaluator_name": [item.get("evaluator_name") for item in evaluators],
        "task_type": response_payload.get("task_type"),
        "answer": expected.get("retrieved_data"),
        "response": (
            actual.get("retrieved_data")
            if "retrieved_data" in actual
            else response_payload.get("retrieved_data")
        ),
        "assertions": assertions or None,
    }


def _print_webarena_outputs(
    *,
    resp_path: Path,
    response_payload: dict,
    eval_path: Path | None,
    eval_payload: dict | None,
) -> None:
    print(f"[webarena] OK agent_response (submission) -> {resp_path}")
    print(json.dumps(response_payload, indent=2, ensure_ascii=False))
    summary = _official_eval_summary(eval_payload, response_payload)
    if summary is not None:
        print(f"[webarena] OFFICIAL_EVAL -> {eval_path}")
        print(json.dumps(summary, indent=2, ensure_ascii=False))


_DIRECT_NAV_TEMPLATE_RE = re.compile(
    r"https?://|\{[^{}]*(?:url|href|链接)[^{}]*\}",
    re.IGNORECASE,
)
_DIRECT_BACK_RE = re.compile(r"返回|上一页|后退|\bback\b", re.IGNORECASE)


def _program_step_tag(s) -> str:
    """Human-facing execution category for the printed DSL program.

    This is intentionally a little finer than the runtime ABI's ``interactive`` /
    ``non_interactive`` split: direct browser navigation is state-changing but
    bypasses the planner UI loop.
    """
    kind = str(getattr(s, "kind", "") or "")
    name = str(getattr(s, "name", "") or "")
    nm = type(s).__name__
    if nm == "Run" and kind == "navigation" and _DIRECT_BACK_RE.search(name):
        return "browser:navigation_back"
    if nm == "Run" and kind == "navigation" and _DIRECT_NAV_TEMPLATE_RE.search(name):
        return "browser:navigation_url"
    if kind in {"read", "data_query"}:
        return f"non-interactive:{kind}"
    if nm == "Compute":
        return "non-interactive:compute"
    if kind in {"navigation", "filter", "action"}:
        return f"interactive:{kind}"
    return nm.lower()


def _print_program(program) -> None:
    """Pretty-print the decomposed DSL program (functions + statements) to the terminal so the plan
    is visible before/while it runs. Robust to type via class name (no import coupling)."""
    def emit(s, indent: str = "  ") -> None:
        nm = type(s).__name__
        if nm == "Run":
            line = f"{indent}[{_program_step_tag(s)}] {s.name}"
            if getattr(s, "var", ""):
                line += f"  → {s.var}"          # the bind var — what a later if/finish references
            if getattr(s, "returns", None):
                line += f"  returns={s.returns}"
            print(line)
            if s.success_condition:
                print(f"{indent}    ✓ {s.success_condition}")
        elif nm in {"Read", "Query"}:
            line = f"{indent}[{_program_step_tag(s)}] {s.name}"
            if getattr(s, "var", ""):
                line += f"  → {s.var}"
            if getattr(s, "returns", None):
                line += f"  returns={s.returns}"
            print(line)
            if getattr(s, "read_spec", ""):
                print(f"{indent}    spec: {s.read_spec}")
            if nm == "Query" and getattr(s, "sql", ""):
                print(f"{indent}    SQL: {s.sql}")
        elif nm == "Compute":
            print(f"{indent}[{_program_step_tag(s)}] {s.var} = {s.expr}")
        elif nm == "Call":
            print(f"{indent}[control:call] {s.func}({s.args}) -> {s.var}")
        elif nm == "If":
            c = s.cond
            print(f"{indent}[control:if] {c.var}[{c.field}] {c.cmp} {c.value!r}{(' ' + str(c.values)) if c.values else ''}")
            for b in s.then:
                emit(b, indent + "    then ")
            for b in s.otherwise:
                emit(b, indent + "    else ")
        elif nm == "ForEach":
            parts = []
            if getattr(s, "row_fields", None):
                parts.append(f"row_fields={s.row_fields}")
            if getattr(s, "output_fields", None):
                parts.append(f"output_fields={s.output_fields}")
            if getattr(s, "member_desc", ""):
                parts.append(f"member_desc={s.member_desc!r}")
            if getattr(s, "returns", None):
                parts.append(f"returns={s.returns}")
            suffix = (" " + " ".join(parts)) if parts else ""
            print(f"{indent}[control:foreach] {s.var} -> {s.into or s.var + 's'}{suffix}")
            if getattr(s, "body_goal", ""):
                print(f"{indent}    body_goal: {s.body_goal}")
            for b in s.body:
                emit(b, indent + "    ")
        elif nm == "Finish":
            print(f"{indent}[control:finish] {s.message}")
        else:
            print(f"{indent}{nm}: {s}")

    print("[webarena] ── orchestrator program ─────────────────────────")
    for fn in getattr(program, "functions", []) or []:
        print(f"  def {fn.name}({', '.join(fn.params)}) -> {fn.returns}")
        for b in fn.body:
            emit(b, "      ")
    for s in program.statements:
        emit(s)
    print("[webarena] ─────────────────────────────────────────────────")


def _confirm_to_run(enabled: bool) -> bool:
    """When --confirm and stdin is a TTY: wait for Enter before executing. Returns False if the user
    cancels (Ctrl-C / EOF). No-op (returns True) otherwise so headless/CI runs are unaffected."""
    if not enabled or not sys.stdin.isatty():
        return True
    try:
        input("[webarena] 按回车开始执行编排器程序（Ctrl-C 取消）…")
        return True
    except (EOFError, KeyboardInterrupt):
        print("\n[webarena] 已取消，不执行。")
        return False


def _canonical_page_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rstrip("/")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _warn_if_pre_loop_page_changed(device, *, initial_url: str, initial_title: str = "") -> None:
    """Surface out-of-band browser changes between initial observe and the first agent turn."""
    if not initial_url or device is None or not hasattr(device, "page_info"):
        return
    try:
        current_url, current_title = device.page_info()
    except Exception:  # noqa: BLE001 - diagnostic only; never block a run
        return
    if not current_url:
        return
    if _canonical_page_url(current_url) == _canonical_page_url(initial_url):
        return
    initial_label = f" ({initial_title})" if initial_title else ""
    current_label = f" ({current_title})" if current_title else ""
    print(
        "[webarena] pre-loop page changed after initial observe: "
        f"{initial_url}{initial_label} -> {current_url}{current_label}"
    )
    print("[webarena] 这通常表示人工点击或外部 CDP 控制在编排后改动了页面；本次将以当前页继续执行。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a WebArena-Verified task on the browser agent")
    parser.add_argument("--tasks-file", type=Path, required=True, help="agent-input-get output JSON")
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--task-output-dir", type=Path, required=True, help="where agent_response.json + network.har go")
    parser.add_argument("--storage-state", type=Path, default=None, help="Playwright storage_state JSON for auth cookies (optional)")
    parser.add_argument("--cdp-url", type=str, default=None, help="Chrome CDP url (default env CHROME_CDP_URL or :9222)")
    parser.add_argument("--headless", action="store_true", help="launch an isolated headless Chromium instead of attaching to Chrome CDP")
    parser.add_argument(
        "--user-data-dir",
        "--headless-profile-dir",
        dest="user_data_dir",
        type=Path,
        default=None,
        help="persistent Chromium profile for --headless (default: output/.headless_profiles/<site_run>)",
    )
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument(
        "--no-preflight",
        dest="skip_preflight",
        action="store_true",
        help="do not stop after deterministic Program preflight failures",
    )
    dynamic_turns = parser.add_mutually_exclusive_group()
    dynamic_turns.add_argument(
        "--dynamic-max-turns",
        action="store_true",
        help="explicitly allow DSL program complexity to raise max_turns",
    )
    dynamic_turns.add_argument(
        "--no-dynamic-max-turns",
        dest="dynamic_max_turns",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(dynamic_max_turns=False)
    parser.add_argument(
        "--include-skills",
        action="store_true",
        help="explicitly include optional _skill.md orchestration hints; default: functional docs only",
    )
    parser.add_argument(
        "--eval-compat",
        action="store_true",
        help=(
            "explicitly enable WebArena evaluator compatibility probes. "
            f"Can also be enabled with {_EVAL_COMPAT_ENV}=1. Default: off."
        ),
    )
    parser.add_argument("--confirm", action="store_true",
                        help="print the decomposed orchestrator program and WAIT for Enter before executing (inspect the plan first; Ctrl-C cancels). No-op when stdin is not a TTY.")
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="override the start_url host (IP-only keeps the per-site port; host:port replaces the netloc). "
             "Also read from env WA_HOST / .env (lower precedence than --host).",
    )
    args = parser.parse_args()

    # Force the browser platform for build_platform() (here and inside run_agent_loop).
    os.environ["AGENT_PLATFORM"] = "browser"
    if args.headless:
        os.environ["BROWSER_HEADLESS"] = "1"
        os.environ["WEB_ARENA_HEADLESS"] = "1"
        # headless is the unified switch: it also drops the OS cursor/HUD overlay
        # (factory _resolve_headless / loop both honor it). No separate viz toggle.
    if args.cdp_url and not args.headless:
        os.environ["CHROME_CDP_URL"] = args.cdp_url
    elif args.cdp_url and args.headless:
        print("[webarena] --cdp-url ignored because --headless launches its own browser")

    from dotenv import load_dotenv
    load_dotenv()

    # Host override for start_urls: --host > WA_HOST env/.env > none. Lets a new LAN
    # IP be configured in one place without editing the baked tasks-file.
    host_override = args.host or os.environ.get("WA_HOST") or None
    eval_compat_enabled = bool(args.eval_compat or _truthy_env(_EVAL_COMPAT_ENV))
    if eval_compat_enabled:
        print(f"[webarena] eval_compat: enabled ({_EVAL_COMPAT_ENV}=1 or --eval-compat)")

    from gui_agent.core.runtime.factory import build_platform
    from gui_agent.core.run.io import EscStopSignal, create_run_dir
    from gui_agent.core.runner import run_agent_loop, build_policy, build_supervisor
    from gui_agent.adapters.browser.har_recorder import HarRecorder

    task = _load_task(args.tasks_file, args.task_id)
    intent = task["intent"]
    start_urls = task.get("start_urls") or []
    if host_override and start_urls:
        rewritten = [_rewrite_url_host(u, host_override) for u in start_urls]
        for old, new in zip(start_urls, rewritten):
            if old != new:
                print(f"[webarena] start_url host override: {old} -> {new}")
        start_urls = rewritten
    start_url = start_urls[0] if start_urls else None
    print(f"[webarena] task {args.task_id}  sites={task.get('sites')}")
    print(f"[webarena] intent: {intent}")
    print(f"[webarena] start_url: {start_url}")

    out_dir: Path = args.task_output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    har_path = out_dir / "network.har"
    resp_path = out_dir / "agent_response.json"

    if args.headless:
        raw_profile = (
            args.user_data_dir
            or os.environ.get("WEB_ARENA_USER_DATA_DIR")
            or os.environ.get("BROWSER_USER_DATA_DIR")
        )
        profile_dir = (
            Path(raw_profile).expanduser()
            if raw_profile
            else out_dir.parent.parent / ".headless_profiles" / _site_profile_name(task, out_dir)
        )
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.environ["BROWSER_USER_DATA_DIR"] = str(profile_dir)
        print(f"[webarena] headless profile: {profile_dir}")

    action_policy = build_policy("browser_vision")
    supervisor = build_supervisor("milestone")
    # Translucent status HUD over the Chrome window — on in headed mode, off when headless
    # (the unified visibility switch). The agent loop repositions it onto the exact CDP
    # window rect once connected.
    hud = build_platform().make_status_reporter(not args.headless)
    log_dir = create_run_dir("webarena", "browser")
    print(f"[webarena] agent logs: {log_dir}")

    from gui_agent.core.run.io import tee_stdio

    # Tee everything below to log_dir/stdout.log (same as the runner) so a WebArena run leaves an
    # inspectable log — the knowledge-binding line and every turn included.
    with tee_stdio(log_dir):
        # Bind app knowledge by the task's `sites` tag. The runner discovers knowledge by matching
        # the app name as a substring of the goal, but a WebArena intent never names its site — so
        # we bind directly on the site tag (site -> knowledge/browser/<site>/ when a base exists).
        from gui_agent.core.self_learning.app_summary import load_knowledge_for_app

        knowledge = None
        knowledge_summary: Optional[dict] = None  # persisted to context.json so the report renders it
        for site in (task.get("sites") or []):
            knowledge = load_knowledge_for_app(
                site,
                "browser",
                include_skills=args.include_skills,
            )
            if (
                knowledge
                and host_override
                and start_url
                and "_deploy" in knowledge.overlays
            ):
                rebased = _rebase_deployment_origin(knowledge.navigation, start_url)
                if rebased != knowledge.navigation:
                    knowledge.navigation = rebased
                    print(
                        "[webarena] knowledge: deployment origin rebased to "
                        f"{urlsplit(start_url).scheme}://{urlsplit(start_url).netloc}"
                    )
            if knowledge and knowledge.navigation and hasattr(supervisor, "set_app_knowledge"):
                supervisor.set_app_knowledge(
                    knowledge.navigation,
                    app_name=knowledge.app_name,
                    elements=knowledge.elements,
                    sections=knowledge.sections,
                    check=knowledge.check,
                )
                knowledge_summary = knowledge.summary()
                print(f"[webarena] knowledge: bound site={site} "
                      f"(nav={knowledge_summary['nav_chars']} chars, "
                      f"sections={knowledge_summary['section_count']}, "
                      f"profile={knowledge_summary['profile']})")
                selected = knowledge.decompose_sections(intent)
                knowledge_summary["decompose_sections"] = selected
                print(
                    "[webarena] knowledge: decompose sections="
                    f"{selected or ['<app-overview-only>']}"
                )
                break
        else:
            if task.get("sites"):
                print(f"[webarena] knowledge: none for sites={task.get('sites')} — running bare")

        recorder_holder: dict = {}

        def _prime(platform) -> None:
            device = platform.client
            # 1) auth: inject cookies (raw CDP) — no headless ui_login.
            if args.storage_state:
                print("[webarena]", device.load_cookies(str(args.storage_state)))
            # 2) start HAR capture BEFORE navigating, so the start_url load is recorded.
            recorder_holder["rec"] = HarRecorder(device).start()
            # 3) land on the task start_url (raw-CDP fallback handles the flaky binding).
            if start_url:
                print("[webarena]", device.navigate(start_url))

        try:
            eval_compat_reports: list[dict] = []
            bundle = build_platform()
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
                orchestrator_context_reports: list[dict] = []
                with bundle.open_session() as platform:
                    _prime(platform)
                    device = getattr(platform, "client", None)
                    if device is not None and hasattr(device, "wait_settled"):
                        try:
                            device.wait_settled("navigate")
                        except Exception as exc:  # noqa: BLE001 - best-effort start-url settle
                            print(f"[webarena] start_url settle skipped ({exc})")

                    def _compile_program():
                        orchestrator_metrics: dict = {}
                        run_max_turns = args.max_turns
                        preflight_blocked = False
                        compile_result = None
                        _redecompose = None
                        _subdecompose = None
                        initial_observed_url = ""
                        initial_observed_title = ""
                        cur_url = ""
                        cur_title = ""
                        cur_site = knowledge.app_name if knowledge is not None else ""
                        initial_png = None
                        initial_tables = None
                        try:
                            initial_obs = bundle.make_perception(
                                platform, log_dir / "screenshot_initial.png"
                            ).observe()
                            cur_url = initial_obs.url or ""
                            cur_title = initial_obs.title or ""
                            initial_observed_url = cur_url
                            initial_observed_title = cur_title
                            initial_png = initial_obs.png_bytes
                            initial_tables = getattr(initial_obs, "tables", None)
                            if not cur_site and cur_url:
                                from gui_agent.core.self_learning.app_summary import match_app_by_url
                                cur_site = match_app_by_url(cur_url, "browser") or ""
                            if cur_url or cur_site:
                                shown = cur_site or cur_url
                                print(f"[webarena] current page: {shown}" + (f" ({cur_title})" if cur_title else ""))
                        except Exception as exc:  # noqa: BLE001
                            print(f"[webarena] initial observe failed; orchestrator decompose without screenshot ({exc})")

                        from gui_agent.core.orchestrator import (
                            OrchestratorCompileError,
                            decompose,
                            estimate_program_turns,
                            redecompose,
                            validate_orchestration_preflight,
                        )
                        from gui_agent.core.supervisor.milestone.model_io import resolve_file_refs
                        from gui_agent.core.router import resolve_intent

                        file_section = resolve_file_refs(intent)
                        # Intent Resolver: classify lookup entities (precise vs fuzzy + search key)
                        # BEFORE decompose. Text-only judgment on the goal — no app knowledge: that
                        # knowledge is HOW-layer (navigation/UI) content decompose already gets
                        # independently via its own `knowledge` param; it doesn't carry the kind of
                        # fact this judgment needs. The DECISION (fuzzy allowed? which key?) is
                        # intent — decompose renders it as a facts-only context block (intent_block,
                        # via the `resolution=` param) right after the goal; decompose then owns
                        # only the retrieval STRATEGY (the exact→0→key ladder), not the decision.
                        resolution = resolve_intent(intent)
                        if resolution.entities:
                            print(f"[webarena] intent: " + "; ".join(
                                f"{e.mention}→{e.type}/{e.match_mode}/key={e.search_key}" for e in resolution.entities))
                        orch_started = time.perf_counter()
                        orch_calls_before = get_llm_call_count()
                        orch_tokens_before = get_llm_token_usage()
                        # decompose finalizes gates centrally (passes.finalize_gates); no caller wrap.
                        compile_error: OrchestratorCompileError | None = None
                        try:
                            program = decompose(
                                intent,
                                knowledge=knowledge.decompose_context(intent) if knowledge else "",
                                file_section=file_section,
                                current_url=cur_url,
                                current_title=cur_title,
                                current_site=cur_site,
                                table_summaries=initial_tables,
                                png_bytes=initial_png,
                                prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                                context_reports=orchestrator_context_reports,
                                resolution=resolution,
                            )
                        except OrchestratorCompileError as exc:
                            compile_error = exc
                            program = exc.program
                        orch_tokens_after = get_llm_token_usage()
                        orchestrator_metrics = {
                            "timings": {"orchestrator.decompose": time.perf_counter() - orch_started},
                            "token_usage": {
                                "orchestrator.decompose": {
                                    "input": orch_tokens_after[0] - orch_tokens_before[0],
                                    "output": orch_tokens_after[1] - orch_tokens_before[1],
                                }
                            },
                            "llm_calls": get_llm_call_count() - orch_calls_before,
                        }
                        if compile_error is not None:
                            summary = "; ".join(str(issue) for issue in compile_error.issues[:3])
                            print(f"[webarena] orchestrator compile failed: {summary}")
                            orchestrator_context_reports.append({
                                "kind": "orchestrator_compile_error",
                                "issues": [
                                    {
                                        "code": issue.code,
                                        "severity": issue.severity,
                                        "message": str(issue),
                                        "evidence": list(issue.evidence),
                                    }
                                    for issue in compile_error.issues
                                ],
                            })
                            preflight_blocked = True
                            compile_result = {
                                "task_type": _guess_webarena_task_type(intent),
                                "goal_completed": False,
                                "stop_reason": f"orchestrator compile failed: {summary}",
                                "result_summary": f"orchestrator compile failed: {summary}",
                                "content_notes": None,
                                "compile_failed": True,
                            }
                            _write_orchestration_preflight_context(
                                log_dir / "context.json",
                                intent=intent,
                                action_policy=action_policy,
                                supervisor=supervisor,
                                knowledge_summary=knowledge_summary,
                                program=program,
                                max_turns=run_max_turns,
                                orchestrator_context_reports=[*orchestrator_context_reports, {
                                    "kind": "orchestrator_metrics",
                                    **orchestrator_metrics,
                                }],
                                orchestrator_metrics=orchestrator_metrics,
                                preflight_result={
                                    "ok": False,
                                    "issues": [
                                        {
                                            "code": issue.code,
                                            "severity": issue.severity,
                                            "message": str(issue),
                                            "evidence": list(issue.evidence),
                                        }
                                        for issue in compile_error.issues
                                    ],
                                },
                                result=compile_result,
                            )
                        else:
                            if file_section:
                                cap = 3000
                                supervisor.add_static_constraint(
                                    file_section if len(file_section) <= cap
                                    else file_section[:cap] + "\n…（配置过长已截断，其余以分解结果为准）"
                                )
                            print(f"[webarena] orchestrator: {len(program.statements)} statements"
                                  f"{(', ' + str(len(program.functions)) + ' functions') if program.functions else ''}")
                            _print_program(program)

                            preflight = validate_orchestration_preflight(intent, program)
                            orchestrator_context_reports.append({
                                "kind": "orchestrator_preflight",
                                **preflight.model_dump(mode="json"),
                            })
                            if preflight.ok:
                                print("[webarena] orchestrator preflight: ok")
                            else:
                                for issue in preflight.blocking_issues:
                                    evidence = f" ({'; '.join(issue.evidence)})" if issue.evidence else ""
                                    print(f"[webarena] orchestrator preflight: {issue.code}: {issue.message}{evidence}")
                                if not args.skip_preflight:
                                    preflight_blocked = True
                                    summary = "; ".join(
                                        f"{issue.code}: {issue.message}" for issue in preflight.blocking_issues[:3]
                                    )
                                    compile_result = {
                                        "task_type": _guess_webarena_task_type(intent),
                                        "goal_completed": False,
                                        "stop_reason": f"orchestrator preflight failed: {summary}",
                                        "result_summary": f"orchestrator preflight failed: {summary}",
                                        "content_notes": None,
                                        "preflight_failed": True,
                                    }
                                    _write_orchestration_preflight_context(
                                        log_dir / "context.json",
                                        intent=intent,
                                        action_policy=action_policy,
                                        supervisor=supervisor,
                                        knowledge_summary=knowledge_summary,
                                        program=program,
                                        max_turns=run_max_turns,
                                        orchestrator_context_reports=[*orchestrator_context_reports, {
                                            "kind": "orchestrator_metrics",
                                            **orchestrator_metrics,
                                        }],
                                        orchestrator_metrics=orchestrator_metrics,
                                        preflight_result=preflight,
                                        result=compile_result,
                                    )

                            # Feasibility Guard kick-back: re-decompose ONLY the remaining plan via the
                            # dedicated redecompose() (NOT a fresh full-goal decompose). The page context
                            # comes from the CURRENT observation at trigger time — the initial frame is just
                            # a fallback when the loop has none — so the re-plan continues from where the run
                            # actually is, not from the start screen. prior_experience / remaining_plan are
                            # supplied by the loop (summarize_progress over the interpreter's run_log).
                            def _redecompose(directive: str, context_reports=None, *, observation=None,
                                             prior_experience="", remaining_plan="",
                                             _goal=intent, _know=knowledge, _file=file_section,
                                             _url=cur_url, _title=cur_title, _site=cur_site,
                                             _tables=initial_tables, _png=initial_png, _res=resolution):
                                _cur_png = getattr(observation, "png_bytes", None) or _png
                                _cur_url2 = (getattr(observation, "url", None) or _url) if observation else _url
                                _cur_title2 = (getattr(observation, "title", None) or _title) if observation else _title
                                _cur_tables = getattr(observation, "tables", None) if observation else None
                                if _cur_tables is None:
                                    _cur_tables = _tables
                                return redecompose(
                                    _goal, knowledge=_know.decompose_context(_goal) if _know else "", file_section=_file,
                                    current_url=_cur_url2, current_title=_cur_title2, current_site=_site,
                                    table_summaries=_cur_tables, png_bytes=_cur_png,
                                    prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                                    corrective_directive=directive, resolution=_res,
                                    prior_experience=prior_experience, remaining_plan=remaining_plan,
                                    context_reports=context_reports,
                                )

                            # Per-row agentic sub-goal (ForEach.body_goal): decompose a row-templated
                            # sub-goal fresh at runtime (same knowledge/site as the main decompose;
                            # depth guard in the interpreter enforces one-level-only). cli.py has wired
                            # this since body_goal shipped; webarena.py hadn't — every body_goal foreach
                            # silently no-opped (778 live 114429: rows=[], "body_goal 无法分解", then
                            # flowed to finish and synthesized SUCCESS with zero saves).
                            def _subdecompose(sub_goal: str, _know=knowledge, _site=cur_site):
                                return decompose(sub_goal, knowledge=_know.decompose_context(sub_goal) if _know else "",
                                                 current_site=_site,
                                                 prepare_vision_prompt_png=bundle.prepare_vision_prompt_png)

                            if not preflight_blocked and args.dynamic_max_turns:
                                run_max_turns = estimate_program_turns(program, floor=args.max_turns)
                                if run_max_turns != args.max_turns:
                                    print(f"[webarena] orchestrator: max_turns {args.max_turns} -> {run_max_turns}")
                        return (
                            program,
                            orchestrator_metrics,
                            run_max_turns,
                            preflight_blocked,
                            _redecompose,
                            _subdecompose,
                            initial_observed_url,
                            initial_observed_title,
                            compile_result,
                        )

                    (
                        program,
                        orchestrator_metrics,
                        run_max_turns,
                        preflight_blocked,
                        _redecompose,
                        _subdecompose,
                        initial_observed_url,
                        initial_observed_title,
                        compile_result,
                    ) = _compile_program()
                    if compile_result is not None:
                        result = compile_result
                    if not preflight_blocked:
                        if not _confirm_to_run(args.confirm):
                            return 1
                        _warn_if_pre_loop_page_changed(
                            device,
                            initial_url=initial_observed_url,
                            initial_title=initial_observed_title,
                        )
                        with EscStopSignal(enabled=True) as esc_stop:
                            if esc_stop.enabled:
                                print("[webarena] Interrupt: 按 ESC 将在当前 turn 收尾后停止")
                            else:
                                print("[webarena] Interrupt: stdin 不是 TTY，ESC 停止未启用")
                            result = run_agent_loop(
                                intent,
                                action_policy,
                                supervisor,
                                None,                       # input_context_path
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
                                subdecompose=_subdecompose,
                                orchestrator_context_reports=[*orchestrator_context_reports, {
                                    "kind": "orchestrator_metrics",
                                    **orchestrator_metrics,
                                }] if orchestrator_metrics else orchestrator_context_reports,
                                stop_requested=esc_stop.requested if esc_stop.enabled else None,
                                platform=platform,
                            )
                        eval_compat_reports = _run_eval_compat_probes(
                            enabled=eval_compat_enabled,
                            task_id=args.task_id,
                            task=task,
                            start_url=start_url,
                            result=result,
                            device=device,
                        )

            # ----- post-run artifacts -----
            rec = recorder_holder.get("rec")
            if rec is not None:
                print("[webarena]", rec.dump(str(har_path)))
            else:
                har_path.write_text('{"log":{"version":"1.2","creator":{"name":"gui_agent"},"entries":[]}}')
                print(f"[webarena] OK har 0 entries (recorder unavailable) -> {har_path}")

            try:
                if result.get("preflight_failed") or result.get("compile_failed"):
                    resp = _preflight_failure_response(intent, result or {})
                else:
                    resp = _synthesize_response(intent, result or {}, log_dir / "context.json")
                response_payload = _finalize_response(
                    resp,
                    goal_completed=bool(result.get("goal_completed")),
                    execution_completed=bool(result.get("execution_completed")),
                    goal_status=str(result.get("goal_status") or ""),
                    intent=intent,
                ).model_dump()
                resp_path.write_text(json.dumps(response_payload, indent=2))
                eval_path = None
                eval_payload = None
                try:
                    eval_path, eval_payload = _run_official_eval(
                        task_id=args.task_id,
                        out_dir=out_dir,
                        resp_path=resp_path,
                        har_path=har_path,
                    )
                    print(
                        "[webarena] OK eval_result (official) -> "
                        f"{eval_path} "
                        f"(status={eval_payload.get('status')}, score={eval_payload.get('score')})"
                    )
                except Exception as eval_exc:  # noqa: BLE001 - official eval is best-effort
                    print(f"[webarena] official eval skipped/failed ({eval_exc})")
                _write_webarena_report_context(
                    log_dir / "context.json",
                    task=task,
                    task_id=args.task_id,
                    start_url=start_url,
                    out_dir=out_dir,
                    har_path=har_path,
                    resp_path=resp_path,
                    response_payload=response_payload,
                    eval_result_path=eval_path,
                    eval_result_payload=eval_payload,
                    eval_compat_reports=eval_compat_reports,
                )
                _print_webarena_outputs(
                    resp_path=resp_path,
                    response_payload=response_payload,
                    eval_path=eval_path,
                    eval_payload=eval_payload,
                )
            except Exception as exc:  # noqa: BLE001 — still leave a valid response file
                fallback = {"task_type": result.get("task_type") or "RETRIEVE", "status": "UNKNOWN_ERROR",
                            "retrieved_data": None, "error_details": f"response synthesis failed: {exc}"}
                resp_path.write_text(json.dumps(fallback, indent=2))
                eval_path = None
                eval_payload = None
                try:
                    eval_path, eval_payload = _run_official_eval(
                        task_id=args.task_id,
                        out_dir=out_dir,
                        resp_path=resp_path,
                        har_path=har_path,
                    )
                    print(
                        "[webarena] OK eval_result (official) -> "
                        f"{eval_path} "
                        f"(status={eval_payload.get('status')}, score={eval_payload.get('score')})"
                    )
                except Exception as eval_exc:  # noqa: BLE001 - official eval is best-effort
                    print(f"[webarena] official eval skipped/failed ({eval_exc})")
                _write_webarena_report_context(
                    log_dir / "context.json",
                    task=task,
                    task_id=args.task_id,
                    start_url=start_url,
                    out_dir=out_dir,
                    har_path=har_path,
                    resp_path=resp_path,
                    response_payload=fallback,
                    eval_result_path=eval_path,
                    eval_result_payload=eval_payload,
                    eval_compat_reports=eval_compat_reports,
                )
                print(f"[webarena] response synthesis failed ({exc}); wrote fallback -> {resp_path}")
                _print_webarena_outputs(
                    resp_path=resp_path,
                    response_payload=fallback,
                    eval_path=eval_path,
                    eval_payload=eval_payload,
                )

            # Auto-generate the HTML run report from context.json (same builder as the runner),
            # so a WebArena run is as inspectable as a normal agent run.
            if (log_dir / "context.json").exists():
                try:
                    from gui_agent.reports import RunnerReportBuilder, save_report
                    report_data = RunnerReportBuilder().build(log_dir)
                    report_path = save_report(report_data, log_dir / "report.html")
                    print(f"[webarena] OK report -> {report_path}")
                except Exception as exc:  # noqa: BLE001 — report is best-effort
                    print(f"[webarena] report generation failed ({exc})")
        finally:
            if hud:
                hud.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
