"""WebArena-Verified entry — runs a task on the existing browser agent loop.

WebArena is framework-agnostic: it hands the agent a minimal task JSON (intent +
start_urls) and grades two artifacts it writes back — ``agent_response.json`` (the
final answer, judged by AgentResponseEvaluator) and ``network.har`` (the recorded
requests, judged by NetworkEventEvaluator). So this entry is THIN: it reuses the
real browser agent (perception + milestone supervisor + executor + visualizer) via
``run_agent_loop`` and only adds the WebArena plumbing around it —

  pre-run  : inject auth cookies (raw CDP) + start HAR capture + navigate start_url
             — all in the ``on_session_open`` hook, on the just-connected session.
  run      : run_agent_loop(intent)  — unchanged, fully reused.
  post-run : dump network.har + synthesize agent_response.json from the run result.

No headless browser anywhere: auth is CDP cookie injection (see device.load_cookies),
the browser is the user's CDP-attached Chrome (bin/launch_chrome_cdp).

Usage:
  AGENT_PLATFORM is forced to "browser" here.
  uv run python -m gui_agent.adapters.browser.webarena \
      --tasks-file output/tasks.json --task-id 124 \
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
import sys
from pathlib import Path
from typing import Optional

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pydantic import BaseModel

# WebArena's required response schema (mirrors the base_template "Final Response
# Format"); the AgentResponseEvaluator normalizes case, so plain str fields are fine.
_TASK_TYPES = ("RETRIEVE", "MUTATE", "NAVIGATE")
_STATUSES = (
    "SUCCESS", "NOT_FOUND_ERROR", "ACTION_NOT_ALLOWED_ERROR",
    "PERMISSION_DENIED_ERROR", "DATA_VALIDATION_ERROR", "UNKNOWN_ERROR",
)


class WAResponse(BaseModel):
    task_type: str          # one of _TASK_TYPES
    status: str             # one of _STATUSES
    retrieved_data: Optional[list] = None  # list (scalars, or objects iff intent asks)
    error_details: Optional[str] = None


def _load_task(tasks_file: Path, task_id: int) -> dict:
    tasks = json.loads(tasks_file.read_text())
    for t in tasks:
        if t.get("task_id") == task_id:
            return t
    ids = [t.get("task_id") for t in tasks]
    raise ValueError(f"task {task_id} not in {tasks_file} (have {ids})")


def _synthesize_response(intent: str, result: dict) -> WAResponse:
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
    sys_msg = (
        "You convert a web agent's run result into WebArena-Verified's required "
        "agent_response JSON. Output exactly: task_type (one of "
        f"{', '.join(_TASK_TYPES)}), status (one of {', '.join(_STATUSES)}), "
        "retrieved_data (a LIST, or null), error_details (string or null).\n"
        "- Follow the OUTPUT FORMAT embedded in the task intent precisely.\n"
        "- For RETRIEVE, retrieved_data must be a list. Use a list of OBJECTS only "
        "when the intent asks for keyed fields (e.g. {\"min\":..,\"max\":..}); "
        "otherwise a list of scalar values. Emit numbers as numbers, not strings.\n"
        "- If the agent did not actually obtain the answer, set status to the best-"
        "fitting error and retrieved_data to null. Do not invent data."
    )
    human = (
        f"Task intent (includes the required output format):\n{intent}\n\n"
        f"Agent task_type guess: {result.get('task_type')}\n"
        f"Goal completed: {result.get('goal_completed')}\n"
        f"Stop reason: {result.get('stop_reason')}\n"
        f"Run summary: {result.get('result_summary')}\n\n"
        f"Collected notes:\n{notes_text}\n\n"
        "Produce the agent_response JSON."
    )
    cfg = resolve_llm_config("output")
    llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)
    return invoke_structured(llm, [SystemMessage(content=sys_msg), HumanMessage(content=human)], WAResponse)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a WebArena-Verified task on the browser agent")
    parser.add_argument("--tasks-file", type=Path, required=True, help="agent-input-get output JSON")
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--task-output-dir", type=Path, required=True, help="where agent_response.json + network.har go")
    parser.add_argument("--storage-state", type=Path, default=None, help="Playwright storage_state JSON for auth cookies (optional)")
    parser.add_argument("--cdp-url", type=str, default=None, help="Chrome CDP url (default env CHROME_CDP_URL or :9222)")
    parser.add_argument("--max-turns", type=int, default=25)
    parser.add_argument("--hud", action="store_true", help="show the translucent status HUD over the Chrome window (macOS)")
    args = parser.parse_args()

    # Force the browser platform for build_platform() (here and inside run_agent_loop).
    os.environ["AGENT_PLATFORM"] = "browser"
    if args.cdp_url:
        os.environ["CHROME_CDP_URL"] = args.cdp_url

    from dotenv import load_dotenv
    load_dotenv()

    from gui_agent.core.factory import build_platform
    from gui_agent.core.run_io import create_run_dir
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

    action_policy = build_policy("browser_vision")
    supervisor = build_supervisor("milestone")
    # Translucent status HUD over the Chrome window (None when --hud absent). The
    # agent loop repositions it onto the exact CDP window rect once connected.
    hud = build_platform().make_status_reporter(args.hud)
    log_dir = create_run_dir("webarena", "browser")
    print(f"[webarena] agent logs: {log_dir}")

    recorder_holder: dict = {}

    def _prime(phone) -> None:
        device = phone.client
        # 1) auth: inject cookies (raw CDP) — no headless ui_login.
        if args.storage_state:
            print("[webarena]", device.load_cookies(str(args.storage_state)))
        # 2) start HAR capture BEFORE navigating, so the start_url load is recorded.
        recorder_holder["rec"] = HarRecorder(device).start()
        # 3) land on the task start_url (raw-CDP fallback handles the flaky binding).
        if start_url:
            print("[webarena]", device.navigate(start_url))

    try:
        result = run_agent_loop(
            intent,
            action_policy,
            supervisor,
            None,                       # input_context_path
            log_dir,
            log_dir / "context.json",
            max_turns=args.max_turns,
            auto_continue=True,
            hud=hud,
            raw_input=intent,
            router=None,
            on_session_open=_prime,
        )

        # ----- post-run artifacts (session already closed; both are offline) -----
        rec = recorder_holder.get("rec")
        if rec is not None:
            print("[webarena]", rec.dump(str(har_path)))
        else:
            har_path.write_text('{"log":{"version":"1.2","creator":{"name":"gui_agent"},"entries":[]}}')
            print(f"[webarena] OK har 0 entries (recorder unavailable) -> {har_path}")

        try:
            resp = _synthesize_response(intent, result or {})
            resp_path.write_text(json.dumps(resp.model_dump(), indent=2))
            print(f"[webarena] OK agent_response -> {resp_path}")
            print(json.dumps(resp.model_dump(), indent=2, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 — still leave a valid response file
            fallback = {"task_type": result.get("task_type") or "RETRIEVE", "status": "UNKNOWN_ERROR",
                        "retrieved_data": None, "error_details": f"response synthesis failed: {exc}"}
            resp_path.write_text(json.dumps(fallback, indent=2))
            print(f"[webarena] response synthesis failed ({exc}); wrote fallback -> {resp_path}")
    finally:
        if hud:
            hud.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
