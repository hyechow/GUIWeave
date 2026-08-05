"""Static MobileWorld coding-orchestrator eval; no emulator is started.

Run:
  uv run python evals/android/orchestrator/test_orchestrator.py
  uv run python evals/android/orchestrator/test_orchestrator.py -j 5
  uv run python evals/android/orchestrator/test_orchestrator.py --group read_compute
  uv run python evals/android/orchestrator/test_orchestrator.py --task MastodonConditionalFavoTask
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from gui_agent.core.orchestrator import generate_code  # noqa: E402
from gui_agent.core.chat.session import route_message  # noqa: E402
from gui_agent.core.router import resolve_intent  # noqa: E402
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app  # noqa: E402
from gui_agent.adapters.android.mobileworld import _android_platform_contract  # noqa: E402

_SHARED_PATH = ROOT / "evals/browser/orchestrator/test_orchestrator.py"
_SPEC = importlib.util.spec_from_file_location("shared_orchestrator_eval", _SHARED_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SHARED = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SHARED)

CASES_FILE = Path(__file__).with_name("cases.json")
evaluate_source = _SHARED.evaluate_source


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def _knowledge(case: dict) -> tuple[str, str]:
    contexts = []
    names = []
    for app in case.get("apps", []):
        knowledge = load_knowledge_for_app(app, "android")
        if knowledge and knowledge.navigation:
            contexts.append(knowledge.orchestrator_context(case["intent"]))
            names.append(app)
    return "\n\n".join(contexts), " + ".join(names)


def _run_sample(
    case: dict[str, Any],
    *,
    sample_index: int,
    knowledge: str,
    app: str,
) -> dict[str, Any]:
    """Generate + grade one sample. Safe for thread-pool workers."""
    if case.get("use_raw_intent"):
        routed_goal = case["intent"]
    else:
        route = route_message(case["intent"], session=[], platform="android")
        routed_goal = str(route.goal or case["intent"])
    resolution = resolve_intent(routed_goal)
    plan = generate_code(
        routed_goal,
        knowledge=knowledge,
        platform_contract=_android_platform_contract(case.get("apps", [])),
        resolution=resolution,
        current_site=app,
    )
    failures = evaluate_source(plan.source, case["contract"])
    if not plan.executable:
        failures.insert(0, "PLAN_NOT_EXECUTABLE")
    return {
        "sample": sample_index,
        "ok": not failures,
        "failures": failures,
        "routed_goal": routed_goal,
        "resolution": resolution.model_dump(mode="json"),
        "source": plan.source,
        "attempts": [
            {
                "diagnostics": [item.render() for item in attempt.diagnostics],
                "run_ok": attempt.run.ok if attempt.run is not None else None,
                "run_error": attempt.run.error if attempt.run is not None else "",
            }
            for attempt in plan.attempts
        ],
    }


def _summary(
    *,
    group_filter: str,
    cases: list[dict[str, Any]],
    results: list[dict[str, Any] | None],
    k: int,
    jobs: int,
) -> dict[str, Any]:
    finished = [item for item in results if item is not None]
    samples = [
        sample
        for item in finished
        for sample in item.get("samples", [])
    ]
    return {
        "group_filter": group_filter,
        "total_cases": len(cases),
        "finished_cases": len(finished),
        "k": k,
        "jobs": jobs,
        "samples_passed": sum(1 for sample in samples if sample.get("ok")),
        "samples_total": len(cases) * k,
        "samples_finished": len(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="all")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=5,
        help="max concurrent case workers (default 5, capped at 5)",
    )
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    jobs = max(1, min(5, args.jobs))
    k = max(1, args.k)
    wanted = set(args.task)
    cases = [
        case for case in load_cases()
        if (args.group == "all" or case["group"] == args.group)
        and (not wanted or case["task_name"] in wanted)
    ]
    if args.list:
        for case in cases:
            print(f"{case['group']:30s} {case['task_name']}")
        return 0

    # Preload knowledge once per unique app set so workers only share strings.
    knowledge_by_key: dict[tuple[str, ...], tuple[str, str]] = {}
    for case in cases:
        key = tuple(case.get("apps", []))
        if key not in knowledge_by_key:
            knowledge_by_key[key] = _knowledge(case)

    output_dir = ROOT / "logs/orchestrator_eval/android" / time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    # Preserve input order in the final report.
    results: list[dict[str, Any] | None] = [None] * len(cases)
    print_lock = threading.Lock()
    write_lock = threading.Lock()
    done = 0

    def work(index: int, case: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        knowledge, app = knowledge_by_key[tuple(case.get("apps", []))]
        samples: list[dict[str, Any]] = []
        for sample_index in range(1, k + 1):
            try:
                samples.append(
                    _run_sample(
                        case,
                        sample_index=sample_index,
                        knowledge=knowledge,
                        app=app,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — isolate worker crashes
                samples.append({
                    "sample": sample_index,
                    "ok": False,
                    "failures": [f"WORKER_ERROR:{type(exc).__name__}: {exc}"],
                    "routed_goal": case.get("intent", ""),
                    "resolution": {},
                    "source": "",
                    "attempts": [],
                })
        return index, {**case, "samples": samples}

    def emit(index: int, result: dict[str, Any]) -> None:
        nonlocal done
        with print_lock:
            for sample in result["samples"]:
                mark = "PASS" if sample["ok"] else "FAIL"
                print(
                    f"[{mark}] {index + 1}/{len(cases)} {result['task_name']} "
                    f"sample={sample['sample']}/{k}",
                    flush=True,
                )
                if sample["failures"]:
                    print(
                        "       " + "; ".join(sample["failures"][:5]),
                        flush=True,
                    )
            done += 1
        with write_lock:
            results[index] = result
            payload = {
                "summary": _summary(
                    group_filter=args.group,
                    cases=cases,
                    results=results,
                    k=k,
                    jobs=jobs,
                ),
                "cases": [item for item in results if item is not None],
            }
            (output_dir / "report.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if jobs == 1 or len(cases) <= 1:
        for index, case in enumerate(cases):
            emit(index, work(index, case)[1])
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(work, index, case): index
                for index, case in enumerate(cases)
            }
            for future in as_completed(futures):
                index, result = future.result()
                emit(index, result)

    finished = [item for item in results if item is not None]
    samples = [
        sample
        for item in finished
        for sample in item.get("samples", [])
    ]
    failed = sum(1 for sample in samples if not sample.get("ok"))
    total = len(cases) * k
    print(f"\n{total - failed}/{total} samples passed")
    print(f"jobs={jobs}")
    print(f"report -> {output_dir / 'report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
