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
             drives each linear milestone; --no-orchestrator keeps the legacy DAG path.
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


def _normalize_retrieved_data_for_intent(data: object, intent: str = "") -> object:
    """Conservatively coerce obvious over-shaped WebArena answers to the requested shape.

    If the task asks for search term(s) only, a read may still produce rows like
    ``{"term": "hollister", "uses": 19}`` because the UI table includes helper columns.
    WebArena's evaluator expects the requested values, not the whole row object. Only flatten
    that narrow case; keep objects for tasks that ask for keyed fields or metrics.
    """
    if not isinstance(data, list) or not data:
        return data
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


def _finalize_response(resp: WAResponse, *, goal_completed: bool = True, intent: str = "") -> WAResponse:
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

    return resp.model_copy(update=updates)


def _load_task(tasks_file: Path, task_id: int) -> dict:
    tasks = json.loads(tasks_file.read_text())
    for t in tasks:
        if t.get("task_id") == task_id:
            return t
    ids = [t.get("task_id") for t in tasks]
    raise ValueError(f"task {task_id} not in {tasks_file} (have {ids})")


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
    for turn in (data.get("turns") or [])[-6:]:
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
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from gui_agent.core.config import resolve_llm_config
    from llm.structured import invoke_structured

    notes = result.get("content_notes") or []
    notes_text = "\n".join(f"- {n}" for n in notes) if notes else "(none collected)"
    evidence_text = _run_evidence_text(context_path)
    human = _WEBARENA_HUMAN.render(
        intent=intent,
        task_type_guess=result.get("task_type"),
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
    parser.add_argument("--no-orchestrator", action="store_true", help="use the legacy milestone DAG path instead of the DSL orchestrator")
    parser.add_argument("--no-dynamic-max-turns", action="store_true", help="do not raise max_turns from DSL program complexity")
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

    from gui_agent.core.runtime.factory import build_platform
    from gui_agent.core.run.io import EscStopSignal, create_run_dir
    from gui_agent.core.runner import run_agent_loop, build_policy, build_supervisor
    from gui_agent.adapters.browser.har_recorder import HarRecorder

    task = _load_task(args.tasks_file, args.task_id)
    intent = task["intent"]
    start_urls = task.get("start_urls") or []
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
            knowledge = load_knowledge_for_app(site, "browser")
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
                      f"sections={knowledge_summary['section_count']})")
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
                program = None
                orchestrator_context_reports: list[dict] = []
                orchestrator_metrics: dict = {}
                run_max_turns = args.max_turns
                with bundle.open_session() as platform:
                    _prime(platform)
                    device = getattr(platform, "client", None)
                    if device is not None and hasattr(device, "wait_settled"):
                        try:
                            device.wait_settled("navigate")
                        except Exception as exc:  # noqa: BLE001 - best-effort start-url settle
                            print(f"[webarena] start_url settle skipped ({exc})")

                    if not args.no_orchestrator:
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
                            decompose,
                            estimate_program_turns,
                            normalize_confirm_read_gates,
                            normalize_precondition_gates,
                        )
                        from gui_agent.core.supervisor.milestone.helpers import resolve_file_refs

                        file_section = resolve_file_refs(intent)
                        orch_started = time.perf_counter()
                        orch_calls_before = get_llm_call_count()
                        orch_tokens_before = get_llm_token_usage()
                        program = normalize_precondition_gates(
                            normalize_confirm_read_gates(
                                decompose(
                                    intent,
                                    knowledge=knowledge.navigation if knowledge else "",
                                    file_section=file_section,
                                    current_url=cur_url,
                                    current_title=cur_title,
                                    current_site=cur_site,
                                    table_summaries=initial_tables,
                                    png_bytes=initial_png,
                                    prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                                    context_reports=orchestrator_context_reports,
                                )
                            )
                        )
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
                        if file_section and hasattr(supervisor, "_global_constraints"):
                            cap = 3000
                            supervisor._global_constraints.append(
                                file_section if len(file_section) <= cap
                                else file_section[:cap] + "\n…（配置过长已截断，其余以分解结果为准）"
                            )
                        print(f"[webarena] orchestrator: {len(program.statements)} statements")
                        if not args.no_dynamic_max_turns:
                            run_max_turns = estimate_program_turns(program, floor=args.max_turns)
                            if run_max_turns != args.max_turns:
                                print(f"[webarena] orchestrator: max_turns {args.max_turns} -> {run_max_turns}")
                    else:
                        print("[webarena] orchestrator: disabled; using legacy milestone DAG")

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
                            orchestrator_context_reports=[*orchestrator_context_reports, {
                                "kind": "orchestrator_metrics",
                                **orchestrator_metrics,
                            }] if orchestrator_metrics else orchestrator_context_reports,
                            stop_requested=esc_stop.requested if esc_stop.enabled else None,
                            platform=platform,
                        )

            # ----- post-run artifacts -----
            rec = recorder_holder.get("rec")
            if rec is not None:
                print("[webarena]", rec.dump(str(har_path)))
            else:
                har_path.write_text('{"log":{"version":"1.2","creator":{"name":"gui_agent"},"entries":[]}}')
                print(f"[webarena] OK har 0 entries (recorder unavailable) -> {har_path}")

            try:
                resp = _synthesize_response(intent, result or {}, log_dir / "context.json")
                response_payload = _finalize_response(
                    resp, goal_completed=bool(result.get("goal_completed")), intent=intent
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
