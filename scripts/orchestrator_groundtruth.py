"""Orchestrator ground-truth benchmark: batch-decompose WebArena goals offline and grade the PLANS
against assertions derived automatically from each task's evaluator reference.

The user's direction (2026-07-02): the runtime bottleneck is the DSL-program layer (router +
decompose) — milestone-level react is basically adequate. WebArena's dataset
(webarena-verified/assets/dataset/webarena-verified.json, 812 tasks × 5 sites) carries per-task
evaluator references that double as PLAN-level ground truth, no hand-written cases needed:

  - AgentResponse expected.task_type=retrieve with retrieved_data → the plan must produce a result
    (returns / data_query / foreach returns) AND have a finish.
  - N NetworkEventEvaluators with N>1 distinct save URLs → the task is multi-target → the plan must
    iterate (foreach), not act once (WebArena 778: 3 variant saves, single-variant plan = 1/3).
  - Router resolution + preflight: the router's semantic decisions must survive into the plan
    (validate_orchestration_preflight).

Per task we sample decompose K times (LLM variance is the disease being measured), grade every
sample with the deterministic gates, and report pass@1 vs pass@K per family + top failure codes.
pass@K >> pass@1 quantifies exactly what sample-and-validate would buy.

Usage:
  uv run python scripts/orchestrator_groundtruth.py --sites shopping_admin --tasks 778 63 708 --k 1
  uv run python scripts/orchestrator_groundtruth.py --sites shopping_admin --hard-only --k 2
Report JSON → logs/orchestrator_groundtruth/<ts>/report.json (plus a console summary).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import os

os.chdir(PROJECT_ROOT)
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import (  # noqa: E402
    ForEach,
    If,
    Program,
    Run,
    decompose,
    validate_orchestration_preflight,
    validate_program,
)
from gui_agent.core.router import resolve_intent  # noqa: E402
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app  # noqa: E402

DATASET = PROJECT_ROOT / "webarena-verified/assets/dataset/webarena-verified.json"
HARD_SETS = {
    "shopping_admin": PROJECT_ROOT / "webarena-verified/output/shopping_admin_hard_tasks.json",
    "shopping": PROJECT_ROOT / "webarena-verified/output/shopping_hard_tasks.json",
}
# dataset site name → knowledge app dir (knowledge/browser/<app>)
SITE_KNOWLEDGE_APP = {"shopping_admin": "shopping_admin", "shopping": "shopping"}


# ---------------------------------------------------------------- ground truth derivation

def derive_ground_truth(task: dict) -> dict:
    """Distill the evaluator reference into plan-level requirements (all deterministic)."""
    evals = task.get("eval") or []
    agent = next((e for e in evals if e.get("evaluator") == "AgentResponseEvaluator"), {})
    expected = agent.get("expected") or {}
    task_type = str(expected.get("task_type") or "").lower()
    wants_data = expected.get("retrieved_data") is not None

    ne_urls: set[str] = set()
    for e in evals:
        if e.get("evaluator") == "NetworkEventEvaluator":
            url = ((e.get("expected") or {}).get("url")) or ""
            if url:
                ne_urls.add(url)
    return {
        "task_type": task_type,
        "wants_retrieved_data": wants_data,
        "n_network_targets": len(ne_urls),
        "network_urls": sorted(ne_urls),
    }


# ---------------------------------------------------------------- program shape helpers

def _iter_runs(stmts: list) -> list[Run]:
    out: list[Run] = []
    for s in stmts:
        if isinstance(s, Run):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_iter_runs(s.then))
            out.extend(_iter_runs(s.otherwise))
        elif isinstance(s, ForEach):
            out.extend(_iter_runs(s.body))
    return out


def _has_foreach(stmts: list) -> bool:
    for s in stmts:
        if isinstance(s, ForEach):
            return True
        if isinstance(s, If) and (_has_foreach(s.then) or _has_foreach(s.otherwise)):
            return True
    return False


def _has_finish(program: Program) -> bool:
    def walk(stmts: list) -> bool:
        for s in stmts:
            if type(s).__name__ == "Finish":
                return True
            if isinstance(s, If) and (walk(s.then) or walk(s.otherwise)):
                return True
            if isinstance(s, ForEach) and walk(s.body):
                return True
        return False

    return walk(program.statements)


def _has_result_source(program: Program) -> bool:
    runs = _iter_runs(program.statements)
    if any(r.returns or r.kind == "data_query" for r in runs):
        return True

    def fe(stmts: list) -> bool:
        for s in stmts:
            if isinstance(s, ForEach) and (s.returns or fe(s.body)):
                return True
            if isinstance(s, If) and (fe(s.then) or fe(s.otherwise)):
                return True
        return False

    return fe(program.statements)


# ---------------------------------------------------------------- grading

def grade_program(program: Program, gt: dict, resolution) -> list[str]:
    """Deterministic plan-level failures (empty = the plan is consistent with ground truth)."""
    fails: list[str] = []

    issues = validate_program(program)
    for i in getattr(issues, "issues", []) or []:
        fails.append(f"VALIDATOR:{i.code}")

    pf = validate_orchestration_preflight(program.goal or "", program, resolution=resolution)
    for i in pf.blocking_issues:
        fails.append(f"PREFLIGHT:{i.code}")

    if gt["task_type"] == "retrieve" and gt["wants_retrieved_data"]:
        if not _has_result_source(program):
            fails.append("GT:RETRIEVE_NO_RESULT_SOURCE")
        if not _has_finish(program):
            fails.append("GT:RETRIEVE_NO_FINISH")

    if gt["n_network_targets"] > 1 and not _has_foreach(program.statements):
        # N distinct expected save/GET targets ⇒ the plan must iterate members; a linear plan can
        # hit at most one (778: 3 variant saves ⇒ foreach over size-28 variants).
        fails.append(f"GT:MULTI_TARGET_{gt['n_network_targets']}_WITHOUT_FOREACH")

    return fails


# ---------------------------------------------------------------- main loop

def run_task(task: dict, knowledge_nav: str, current_site: str, k: int) -> dict:
    goal = task["intent"]
    gt = derive_ground_truth(task)
    resolution = None
    res_err = ""
    try:
        resolution = resolve_intent(goal)
    except Exception as e:  # noqa: BLE001 — grade the samples without router coverage
        res_err = f"{type(e).__name__}: {e}"

    samples: list[dict] = []
    for _ in range(k):
        t0 = time.time()
        try:
            program = decompose(
                goal,
                knowledge=knowledge_nav,
                current_site=current_site,
                resolution=resolution,
            )
            fails = grade_program(program, gt, resolution)
            samples.append({
                "ok": not fails,
                "fails": fails,
                "seconds": round(time.time() - t0, 1),
                "n_statements": len(program.statements),
                "has_foreach": _has_foreach(program.statements),
            })
        except Exception as e:  # noqa: BLE001 — a decompose crash is itself a failed sample
            samples.append({
                "ok": False,
                "fails": [f"DECOMPOSE_ERROR:{type(e).__name__}"],
                "seconds": round(time.time() - t0, 1),
                "error": str(e)[:300],
            })

    return {
        "task_id": task["task_id"],
        "intent": goal,
        "ground_truth": gt,
        "resolution_error": res_err,
        "resolution": (
            [e.model_dump() for e in resolution.entities] if resolution is not None else None
        ),
        "samples": samples,
        "pass_at_1": bool(samples and samples[0]["ok"]),
        "pass_at_k": any(s["ok"] for s in samples),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--sites", nargs="+", default=["shopping_admin"])
    ap.add_argument("--tasks", nargs="*", type=int, help="explicit task ids (else all site tasks)")
    ap.add_argument("--hard-only", action="store_true", help="intersect with the hard-tasks set")
    ap.add_argument("--k", type=int, default=1, help="decompose samples per task")
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel tasks (LLM calls are IO-bound; 4-8 is safe)")
    ap.add_argument("--limit", type=int, default=0, help="cap number of tasks (0 = no cap)")
    args = ap.parse_args()

    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    tasks = [t for t in dataset if t.get("sites") == list(args.sites) or set(t.get("sites", [])) == set(args.sites)]
    if args.hard_only:
        hard_ids: set[int] = set()
        for site in args.sites:
            hf = HARD_SETS.get(site)
            if hf and hf.exists():
                hard_ids |= {t["task_id"] for t in json.loads(hf.read_text(encoding="utf-8"))}
        tasks = [t for t in tasks if t["task_id"] in hard_ids]
    if args.tasks:
        want = set(args.tasks)
        tasks = [t for t in tasks if t["task_id"] in want]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("no tasks matched")
        return 1

    app = SITE_KNOWLEDGE_APP.get(args.sites[0], args.sites[0])
    k_obj = load_knowledge_for_app(app, "browser")
    knowledge_nav = k_obj.navigation if k_obj else ""
    current_site = k_obj.app_name if k_obj else args.sites[0]

    out_dir = PROJECT_ROOT / "logs/orchestrator_groundtruth" / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"── Orchestrator Ground-Truth Benchmark ── {len(tasks)} tasks × k={args.k}"
          f" × workers={args.workers} → {out_dir}")

    results: list[dict] = []
    fail_counter: Counter[str] = Counter()
    done = 0
    lock = threading.Lock()  # guards results/counter/print/report across worker completions

    def _record(r: dict) -> None:
        nonlocal done
        with lock:
            done += 1
            results.append(r)
            for s in r["samples"]:
                for f in s["fails"]:
                    fail_counter[re.sub(r"_\d+_", "_N_", f)] += 1
            mark = "✅" if r["pass_at_1"] else ("🔁" if r["pass_at_k"] else "❌")
            first = r["samples"][0] if r["samples"] else {}
            print(f"  [{done:>2}/{len(tasks)}] {mark} {r['task_id']:>4}  {r['intent'][:60]}"
                  f"  {'|' + ';'.join(first.get('fails', [])[:3]) if first.get('fails') else ''}",
                  flush=True)
            results.sort(key=lambda t: t["task_id"])
            (out_dir / "report.json").write_text(
                json.dumps({"tasks": results}, ensure_ascii=False, indent=1), encoding="utf-8"
            )

    if args.workers <= 1:
        for task in tasks:
            _record(run_task(task, knowledge_nav, current_site, args.k))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(run_task, task, knowledge_nav, current_site, args.k) for task in tasks]
            for fut in as_completed(futs):
                _record(fut.result())

    n = len(results)
    p1 = sum(r["pass_at_1"] for r in results)
    pk = sum(r["pass_at_k"] for r in results)
    print(f"\npass@1: {p1}/{n} ({p1 / n:.0%})   pass@{args.k}: {pk}/{n} ({pk / n:.0%})")
    if fail_counter:
        print("top failure codes:")
        for code, cnt in fail_counter.most_common(10):
            print(f"  {cnt:>3}× {code}")
    summary = {
        "n": n, "k": args.k, "pass_at_1": p1, "pass_at_k": pk,
        "failure_codes": dict(fail_counter.most_common()),
    }
    (out_dir / "report.json").write_text(
        json.dumps({"summary": summary, "tasks": results}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"report → {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
