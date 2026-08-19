"""WebArena-Verified harness for GUIWeave Tool Agent Master.

WebArena is framework-agnostic: it hands the agent a minimal task JSON (intent +
start_urls) and grades two artifacts it writes back — ``agent_response.json`` (the
final answer, judged by AgentResponseEvaluator) and ``network.har`` (the recorded
requests, judged by NetworkEventEvaluator). This entry reuses the browser Tool Agent
adapter and adds the WebArena plumbing around it:

  pre-run  : inject auth cookies (raw CDP) + start HAR capture + navigate start_url
             on the just-connected session.
  run      : execute the task intent with Tool Agent Master and visual Workers.
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

"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pydantic import BaseModel

from gui_agent.core.runtime.result import AgentResult, failed_result
from gui_agent.prompts import load_prompt, load_prompt_text

# WebArena's required response schema (mirrors the base_template "Final Response
# Format"); the AgentResponseEvaluator normalizes case, so plain str fields are fine.
_TASK_TYPES = ("RETRIEVE", "MUTATE", "NAVIGATE")
_STATUSES = (
    "SUCCESS", "NOT_FOUND_ERROR", "ACTION_NOT_ALLOWED_ERROR",
    "PERMISSION_DENIED_ERROR", "DATA_VALIDATION_ERROR", "UNKNOWN_ERROR",
)
_EVAL_COMPAT_ENV = "WEBARENA_EVAL_COMPAT"
_TOOL_AGENT_MAX_TURNS = 50
_MAX_TURNS = 50
_RESETTABLE_CONTAINERS = {
    "shopping_admin": "webarena_verified_shopping_admin",
}
_SAFE_REMOTE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_SAFE_REMOTE_TOKEN = re.compile(r"^[A-Za-z0-9._/:=@-]+$")

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
    the intended RETRIEVE answer. A one-field Read result can yield rows
    ``[{"material": "cotton"}, {"material": "fleece"}]``; WebArena expects ``[cotton, fleece]``,
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


def _dedupe_scalars(values: list) -> list:
    """Drop exact duplicates from a scalar list, preserving order.

    A RETRIEVE answer is a set of distinct values: scroll traversal can transcribe
    the same record in several windows, so the collection may hold duplicate rows
    that the evaluator counts as extras (live task 21 returned catso/michelle twice
    and scored 0 despite matching 4/4). Never touch objects — a keyed output may
    legitimately repeat a scalar across records.
    """
    seen: set[str] = set()
    out: list = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _normalize_retrieved_data_for_intent(data: object, intent: str = "") -> object:
    """Conservatively coerce obvious over-shaped WebArena answers to the requested shape.

    If the task asks for search term(s) only, a read may still produce rows like
    ``{"term": "hollister", "uses": 19}`` because the UI table includes helper columns.
    WebArena's evaluator expects the requested values, not the whole row object. Only flatten
    that narrow case; keep objects for tasks that ask for keyed fields or metrics.
    """
    if not isinstance(data, list) or not data:
        return data
    if all(not isinstance(item, dict) for item in data):
        # Already the intended scalar answer shape. A RETRIEVE answer is a set of
        # distinct values; scroll traversal can transcribe the same record in
        # several windows, so dedupe (live task 21 returned names twice and scored
        # 0 despite matching 4/4).
        return _dedupe_scalars(data)
    # General: unwrap a single-column row list to scalars (any intent — the wrapper is never wanted).
    single_col = _single_column_scalars(data)
    if single_col is not None:
        return _dedupe_scalars(single_col)
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
        return _dedupe_scalars(scalars)
    return data


def _runtime_completion_accepted(result: AgentResult) -> bool:
    """Whether runtime reached a terminal state sufficient for a mutation submission.

    A completed phase means the Program ended deliberately. Verification distinguishes a
    confirmed post-state from a reliably dispatched side effect without an authoritative
    post-state channel.
    """
    return result.phase == "completed"


def _finalize_response(
    resp: WAResponse,
    *,
    phase: str = "completed",
    verification: str | None = "confirmed",
    intent: str = "",
) -> WAResponse:
    """Apply deterministic WebArena response invariants after LLM synthesis.

    A RETRIEVE task counts as success when the Program completed and produced a non-empty list answer.
    Verification remains evidence quality; it does not reinterpret a completed coding result.
    The typed terminal itself is failed when, for example, a required read came back empty.
    """
    task_type = (resp.task_type or "").upper()
    status = (resp.status or "").upper()
    retrieved_data = _normalize_retrieved_data_for_intent(resp.retrieved_data, intent)
    updates: dict[str, object] = {"task_type": task_type, "status": status}
    if retrieved_data is not resp.retrieved_data:
        updates["retrieved_data"] = retrieved_data

    completed = phase == "completed"
    retrieve_invalid = task_type == "RETRIEVE" and (
        not completed or not isinstance(retrieved_data, list) or not retrieved_data
    )
    if retrieve_invalid and status == "SUCCESS":
        updates.update({
            "status": "NOT_FOUND_ERROR",
            "retrieved_data": None,
            "error_details": (
                resp.error_details
                or ("Run did not reach completed phase." if not completed
                    else "No non-empty retrieved_data list was produced for this RETRIEVE task.")
            ),
        })

    # MUTATE accepts either confirmed effect or a completed terminal dispatch. It still rejects
    # interrupted programs and empty/incomplete foreach runs (778 live 114429), which have neither.
    mutation_completed = phase == "completed"
    if task_type == "MUTATE" and status == "SUCCESS" and not mutation_completed:
        updates.update({
            "status": "UNKNOWN_ERROR",
            "error_details": resp.error_details or "Run did not reach completed phase (mutation not performed).",
        })

    return resp.model_copy(update=updates)


def _webarena_task_type_from_result(intent: str, result: AgentResult) -> str:
    effect = str((result.orchestrator or {}).get("effect") or "").strip()
    effect_type = {
        "mutation": "MUTATE",
        "data": "RETRIEVE",
        "ui_state": "NAVIGATE",
    }.get(effect)
    if effect_type is not None:
        return effect_type
    task_type = str(result.task_type or "").strip().upper()
    if task_type in _TASK_TYPES:
        return task_type
    return _guess_webarena_task_type(intent)


def _completed_mutate_response(intent: str, result: AgentResult) -> WAResponse | None:
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
    if any(marker in text for marker in mutate_markers):
        return "MUTATE"
    if any(marker in text for marker in navigate_markers):
        return "NAVIGATE"
    if any(marker in text for marker in retrieve_markers):
        return "RETRIEVE"
    return "RETRIEVE"


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


def _run_reset_ssh(host: str, port: int, *remote_args: str) -> str:
    """Run one fixed-shape Docker command on the explicitly configured reset host."""

    if not _SAFE_REMOTE_HOST.fullmatch(host):
        raise ValueError(f"invalid reset SSH host: {host!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid reset SSH port: {port}")
    for value in remote_args:
        if not _SAFE_REMOTE_TOKEN.fullmatch(value):
            raise ValueError(f"unsafe remote reset argument: {value!r}")
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(port),
            f"root@{host}",
            *remote_args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def _wait_for_env_ctrl_ready(url: str, *, timeout: float) -> None:
    """Wait until the recreated container reports all services healthy."""

    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error = "environment control endpoint did not respond"
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("success") is True:
                return
            last_error = str(payload.get("error") or payload.get("message") or payload)
        except Exception as exc:  # noqa: BLE001 - readiness polling records the last cause
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise TimeoutError(
        f"reset instance did not become ready within {timeout:.0f}s: {last_error}"
    )


def _wait_for_site_ready(url: str, *, timeout: float) -> None:
    """Warm the real application endpoint after process-level health succeeds."""

    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error = "site did not respond"
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=10) as response:
                response.read(1)
                if 200 <= response.status < 500:
                    return
                last_error = f"HTTP {response.status}"
        except Exception as exc:  # noqa: BLE001 - readiness polling records the last cause
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise TimeoutError(
        f"reset site did not become ready within {timeout:.0f}s: {last_error}"
    )


def _reset_webarena_instance(
    *,
    site: str,
    start_url: str,
    ssh_host: str | None = None,
    ssh_port: int = 2222,
    timeout: float = 180,
) -> dict[str, object]:
    """Recreate a supported instance from WebArena's canonical container config."""

    container = _RESETTABLE_CONTAINERS.get(site)
    if container is None:
        raise ValueError(f"instance reset is not configured for site {site!r}")
    parts = urlsplit(start_url)
    host = ssh_host or parts.hostname
    if not host or not parts.scheme or not parts.netloc:
        raise ValueError(f"cannot derive reset host/origin from start_url {start_url!r}")
    origin = f"{parts.scheme}://{parts.netloc}/"

    from webarena_verified.environments.container.config import get_container_config
    from webarena_verified.types.task import WebArenaSite

    reset_config = get_container_config(site=WebArenaSite(site))
    site_port = parts.port or reset_config.host_port
    env_ctrl_host_port = reset_config.host_env_ctrl_port
    if site_port is None or env_ctrl_host_port is None:
        raise ValueError(f"canonical reset ports are unavailable for site {site!r}")
    _run_reset_ssh(host, ssh_port, "docker", "rm", "-f", container)
    run_args = [
        "docker",
        "run",
        "-d",
        "--name",
        container,
        "-p",
        f"{site_port}:{reset_config.container_port}",
        "-p",
        f"{env_ctrl_host_port}:{reset_config.env_ctrl_port}",
    ]
    for volume_name, mount_path in reset_config.volumes.items():
        run_args.extend(("-v", f"{volume_name}:{mount_path}"))
    run_args.extend(
        (
            "-e",
            f"WA_ENV_CTRL_EXTERNAL_SITE_URL={origin}",
            reset_config.docker_img,
        )
    )
    container_id = _run_reset_ssh(host, ssh_port, *run_args)
    ready_url = f"http://{host}:{env_ctrl_host_port}/status"
    _wait_for_env_ctrl_ready(ready_url, timeout=timeout)
    _wait_for_site_ready(start_url, timeout=timeout)
    return {
        "site": site,
        "host": host,
        "container": container,
        "image": reset_config.docker_img,
        "container_id": container_id,
        "strategy": "webarena_canonical_config",
        "ready_url": ready_url,
        "site_url": start_url,
    }


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
    result: AgentResult,
    device: object | None,
) -> list[dict]:
    if not enabled:
        return []
    if not (
        result.phase == "completed"
        and result.verification == "confirmed"
    ):
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
    """Return a compact tail of Tool Agent trace events for synthesis diagnostics."""
    if context_path is None or not context_path.exists():
        return "(unavailable)"
    trace_path = context_path.parent / "tool_agent_trace.json"
    if not trace_path.exists():
        return "(none)"
    try:
        data = json.loads(trace_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - best-effort diagnostic context
        return f"(unavailable: {exc})"
    events = data if isinstance(data, list) else data.get("trace") or data.get("events") or []
    return "\n".join(
        json.dumps(event, ensure_ascii=False, default=str)
        for event in events[-6:]
        if isinstance(event, dict)
    ) or "(none)"


def _synthesize_response(
    intent: str,
    result: AgentResult,
    context_path: Path | None = None,
) -> WAResponse:
    """Map Tool Agent's typed result to WebArena's response schema."""
    completed_mutate = _completed_mutate_response(intent, result)
    if completed_mutate is not None:
        return completed_mutate
    orchestrator = result.orchestrator or {}
    platform_rejections = orchestrator.get("platform_rejections") or []
    if (
        result.phase != "completed"
        and orchestrator.get("kind") == "tool_agent"
        and platform_rejections
    ):
        latest = platform_rejections[-1]
        message = (
            str(latest.get("message") or "").strip()
            if isinstance(latest, dict)
            else ""
        )
        return WAResponse(
            task_type=_webarena_task_type_from_result(intent, result),
            status="ACTION_NOT_ALLOWED_ERROR",
            retrieved_data=None,
            error_details=message or "The platform rejected the requested action.",
        )
    if (
        result.phase == "completed"
        and _webarena_task_type_from_result(intent, result) == "NAVIGATE"
    ):
        return WAResponse(
            task_type="NAVIGATE",
            status="SUCCESS",
            retrieved_data=None,
            error_details=None,
        )
    if (
        result.phase == "completed"
        and (result.orchestrator or {}).get("kind") == "tool_agent"
        and _webarena_task_type_from_result(intent, result) == "RETRIEVE"
    ):
        try:
            retrieved = json.loads(result.output)
        except (TypeError, json.JSONDecodeError):
            retrieved = None
        payload = (
            retrieved
            if isinstance(retrieved, list)
            else [retrieved]
            if retrieved is not None
            else None
        )
        return WAResponse(
            task_type="RETRIEVE",
            status="SUCCESS" if payload is not None else "NOT_FOUND_ERROR",
            retrieved_data=payload,
            error_details=None if payload is not None else "Runtime returned no JSON value.",
        )

    from langchain_core.messages import HumanMessage, SystemMessage
    from gui_agent.core.config import resolve_llm_config
    from llm.provider_config import build_chat_model
    from llm.structured import invoke_structured

    evidence_text = _run_evidence_text(context_path)
    human = _WEBARENA_HUMAN.render(
        intent=intent,
        task_type_guess=_webarena_task_type_from_result(intent, result),
        phase=result.phase,
        verification=result.verification,
        summary=result.summary,
        output=result.output,
        data_context_text="Read/Compute outputs and evidence are included below.",
        evidence_text=evidence_text,
    )
    cfg = resolve_llm_config("output")
    llm = build_chat_model(cfg)
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
    reset_requested: bool = False,
    reset_details: dict[str, object] | None = None,
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
        "instance_reset": {
            "requested": reset_requested,
            "completed": reset_details is not None,
            **{
                key: reset_details[key]
                for key in ("site", "host", "container", "image", "ready_url", "site_url")
                if reset_details is not None and key in reset_details
            },
        },
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a WebArena-Verified task with Tool Agent Master"
    )
    parser.add_argument("--tasks-file", type=Path, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--task-output-dir", type=Path, required=True)
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=None,
        help="Playwright storage_state JSON for auth cookies",
    )
    parser.add_argument("--cdp-url", default=None, help="Chrome CDP URL")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="launch isolated Chromium instead of attaching over CDP",
    )
    parser.add_argument(
        "--user-data-dir",
        "--headless-profile-dir",
        dest="user_data_dir",
        type=Path,
        default=None,
        help="persistent Chromium profile for --headless",
    )
    parser.add_argument("--max-turns", type=int, default=_TOOL_AGENT_MAX_TURNS)
    parser.add_argument(
        "--perception",
        choices=("vision-only", "enhanced"),
        default="enhanced",
    )
    parser.add_argument(
        "--multi-action",
        "--tool-agent-multi-action",
        dest="multi_action",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="allow an ordered 1-5 action envelope from a Worker decision",
    )
    parser.add_argument(
        "--include-skills",
        action="store_true",
        help="include optional site _skill.md hints",
    )
    parser.add_argument(
        "--eval-compat",
        action="store_true",
        help=f"enable evaluator compatibility probes (or {_EVAL_COMPAT_ENV}=1)",
    )
    parser.add_argument(
        "--reset-instance",
        action="store_true",
        help="recreate the supported remote task container before the run",
    )
    parser.add_argument("--reset-ssh-host", default=None)
    parser.add_argument("--reset-ssh-port", type=int, default=2222)
    parser.add_argument("--reset-timeout", type=float, default=180)
    parser.add_argument(
        "--host",
        default=None,
        help="override the start_url host; IP-only preserves the original port",
    )
    return parser


def _write_tool_agent_failure_context(
    context_path: Path,
    *,
    intent: str,
    result: AgentResult,
    knowledge_summary: dict | None,
) -> None:
    """Persist an inspectable context when setup or execution raises early."""
    from gui_agent.core.schemas import PolicyContext

    context = PolicyContext(
        goal=intent,
        supervisor_policy_name="tool_agent.master",
        action_policy_name="tool_agent.worker",
        platform="browser",
        raw_input=intent,
    )
    context.knowledge = knowledge_summary
    context.outcome = result.to_program_outcome()
    context.orchestrator = {
        "kind": "tool_agent",
        "effect": {
            "MUTATE": "mutation",
            "NAVIGATE": "ui_state",
        }.get(result.task_type or "", "data"),
    }
    context_path.write_text(context.model_dump_json(indent=2), encoding="utf-8")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if not 1 <= args.max_turns <= _MAX_TURNS:
        parser.error(f"--max-turns must be between 1 and {_MAX_TURNS}")

    os.environ["AGENT_PLATFORM"] = "browser"
    if args.headless:
        os.environ["BROWSER_HEADLESS"] = "1"
        os.environ["WEB_ARENA_HEADLESS"] = "1"
    elif args.cdp_url:
        os.environ["CHROME_CDP_URL"] = args.cdp_url

    from dotenv import load_dotenv

    load_dotenv()
    host_override = args.host or os.environ.get("WA_HOST") or None
    eval_compat_enabled = bool(args.eval_compat or _truthy_env(_EVAL_COMPAT_ENV))

    from gui_agent.adapters.browser.har_recorder import HarRecorder
    from gui_agent.core.runtime.io import create_run_dir, tee_stdio
    from gui_agent.core.runtime.factory import build_platform
    from gui_agent.core.tool_agent.result import execute_tool_agent

    task = _load_task(args.tasks_file, args.task_id)
    intent = str(task["intent"])
    start_urls = list(task.get("start_urls") or [])
    if host_override:
        rewritten = [_rewrite_url_host(url, host_override) for url in start_urls]
        for old, new in zip(start_urls, rewritten):
            if old != new:
                print(f"[webarena] start_url host override: {old} -> {new}")
        start_urls = rewritten
    start_url = start_urls[0] if start_urls else None

    out_dir = args.task_output_dir
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
            else out_dir.parent.parent
            / ".headless_profiles"
            / _site_profile_name(task, out_dir)
        )
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.environ["BROWSER_USER_DATA_DIR"] = str(profile_dir)

    log_dir = create_run_dir("tool_agent", f"webarena/browser/{args.perception}")
    # WebArena loads auth before navigating to its case-owned start URL.
    bundle = build_platform("browser", start_url=None)
    hud = bundle.make_status_reporter(not args.headless)
    reset_details: dict[str, object] | None = None

    print(f"[webarena] task {args.task_id} sites={task.get('sites')}")
    print(f"[webarena] intent: {intent}")
    print(f"[webarena] start_url: {start_url}")
    print(f"[webarena] agent logs: {log_dir}")

    with tee_stdio(log_dir):
        try:
            if args.reset_instance:
                sites = [
                    str(site).strip()
                    for site in (task.get("sites") or [])
                    if str(site).strip()
                ]
                if len(sites) != 1 or start_url is None:
                    raise ValueError(
                        "--reset-instance requires exactly one site and one start_url"
                    )
                reset_details = _reset_webarena_instance(
                    site=sites[0],
                    start_url=start_url,
                    ssh_host=args.reset_ssh_host,
                    ssh_port=args.reset_ssh_port,
                    timeout=args.reset_timeout,
                )

            from gui_agent.core.self_learning.app_summary import load_knowledge_for_app

            knowledge = None
            knowledge_summary: dict | None = None
            for site in task.get("sites") or []:
                candidate = load_knowledge_for_app(
                    site,
                    "browser",
                    include_skills=args.include_skills,
                )
                if candidate is None or not candidate.navigation:
                    continue
                knowledge = candidate
                if host_override and start_url and "_deploy" in knowledge.overlays:
                    knowledge.navigation = _rebase_deployment_origin(
                        knowledge.navigation, start_url
                    )
                    knowledge.deployment = _rebase_deployment_origin(
                        knowledge.deployment, start_url
                    )
                knowledge_summary = knowledge.summary()
                knowledge_summary["orchestrator_sections"] = (
                    knowledge.orchestrator_sections(intent)
                )
                print(f"[webarena] knowledge: bound site={site}")
                break

            result: AgentResult | None = None
            eval_compat_reports: list[dict] = []
            recorder = None
            setup = bundle.setup_check()
            for line in setup.lines:
                print(line)
            if not setup.ok:
                result = failed_result(
                    intent,
                    f"环境检查未通过：{setup.summary}",
                    task_type=_guess_webarena_task_type(intent),
                    failure_kind="environment",
                )
                _write_tool_agent_failure_context(
                    log_dir / "context.json",
                    intent=intent,
                    result=result,
                    knowledge_summary=knowledge_summary,
                )
            else:
                try:
                    with bundle.open_session() as platform:
                        device = platform.client
                        if args.storage_state:
                            print(
                                "[webarena]",
                                device.load_cookies(str(args.storage_state)),
                            )
                        recorder = HarRecorder(device).start()
                        if start_url:
                            print("[webarena]", device.navigate(start_url))
                            if hasattr(device, "eval_js"):
                                try:
                                    device.eval_js("window.scrollTo(0, 0); true")
                                except Exception as exc:  # noqa: BLE001
                                    print(f"[webarena] viewport reset skipped ({exc})")
                        if hasattr(device, "wait_settled"):
                            device.wait_settled("navigate")
                        if hud is not None and hasattr(hud, "reposition"):
                            bounds = (
                                device.window_bounds()
                                if hasattr(device, "window_bounds")
                                else None
                            )
                            if bounds:
                                from gui_agent.core.ui.hud import dock_rect

                                hud.reposition(*dock_rect(*bounds))
                        page_url = page_title = ""
                        if hasattr(device, "page_info"):
                            page_url, page_title = device.page_info()
                        print(
                            "[webarena][tool-agent] "
                            f"perception={args.perception} multi_action={args.multi_action}"
                        )
                        result, _presentation = execute_tool_agent(
                            intent=intent,
                            bundle=bundle,
                            session=platform,
                            log_dir=log_dir,
                            perception_mode=args.perception,
                            max_turns=args.max_turns,
                            allow_multi_action=args.multi_action,
                            fallback_task_type=_guess_webarena_task_type(intent),
                            knowledge_summary=knowledge_summary,
                            knowledge=(
                                knowledge.orchestrator_context(intent)
                                if knowledge is not None
                                else ""
                            ),
                            worker_knowledge=(
                                knowledge.worker_context()
                                if knowledge is not None
                                else ""
                            ),
                            access_context=(
                                knowledge.deployment if knowledge is not None else ""
                            ),
                            page_url=page_url,
                            page_title=page_title,
                            hud=hud,
                            raw_input=intent,
                        )
                        eval_compat_reports = _run_eval_compat_probes(
                            enabled=eval_compat_enabled,
                            task_id=args.task_id,
                            task=task,
                            start_url=start_url,
                            result=result,
                            device=device,
                        )
                except Exception as exc:  # noqa: BLE001
                    print(f"[webarena] Tool Agent run failed: {exc}")
                    result = failed_result(
                        intent,
                        f"Tool Agent execution failed: {exc}",
                        task_type=_guess_webarena_task_type(intent),
                        failure_kind="runtime",
                    )
                    _write_tool_agent_failure_context(
                        log_dir / "context.json",
                        intent=intent,
                        result=result,
                        knowledge_summary=knowledge_summary,
                    )

            if recorder is not None:
                print("[webarena]", recorder.dump(str(har_path)))
            else:
                har_path.write_text(
                    '{"log":{"version":"1.2","creator":{"name":"guiweave"},"entries":[]}}',
                    encoding="utf-8",
                )
            if result is None:
                raise RuntimeError("WebArena run ended without AgentResult")

            try:
                response = _finalize_response(
                    _synthesize_response(intent, result, log_dir / "context.json"),
                    phase=result.phase,
                    verification=result.verification,
                    intent=intent,
                )
                response_payload = response.model_dump()
            except Exception as exc:  # noqa: BLE001
                response_payload = {
                    "task_type": result.task_type or "RETRIEVE",
                    "status": "UNKNOWN_ERROR",
                    "retrieved_data": None,
                    "error_details": f"response synthesis failed: {exc}",
                }
            resp_path.write_text(
                json.dumps(response_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            eval_path = None
            eval_payload = None
            try:
                eval_path, eval_payload = _run_official_eval(
                    task_id=args.task_id,
                    out_dir=out_dir,
                    resp_path=resp_path,
                    har_path=har_path,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[webarena] official eval skipped/failed ({exc})")

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
                reset_requested=args.reset_instance,
                reset_details=reset_details,
            )
            _print_webarena_outputs(
                resp_path=resp_path,
                response_payload=response_payload,
                eval_path=eval_path,
                eval_payload=eval_payload,
            )

            if (log_dir / "context.json").exists():
                try:
                    from gui_agent.reports import RunnerReportBuilder, save_report

                    report_path = save_report(
                        RunnerReportBuilder().build(log_dir),
                        log_dir / "report.html",
                    )
                    print(f"[webarena] OK report -> {report_path}")
                except Exception as exc:  # noqa: BLE001
                    print(f"[webarena] report generation failed ({exc})")
        finally:
            if hud is not None:
                hud.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
