"""Static MobileWorld coding-orchestrator eval; no emulator is started.

Run:
  uv run python evals/android/orchestrator/test_orchestrator.py
  uv run python evals/android/orchestrator/test_orchestrator.py --group read_compute
  uv run python evals/android/orchestrator/test_orchestrator.py --task MastodonConditionalFavoTask
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from gui_agent.core.orchestrator import generate_code  # noqa: E402
from gui_agent.core.chat.session import route_message  # noqa: E402
from gui_agent.core.router import resolve_intent  # noqa: E402
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app  # noqa: E402

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
    for app in case.get("apps", []):
        knowledge = load_knowledge_for_app(app, "android")
        if knowledge and knowledge.navigation:
            return knowledge.orchestrator_context(case["intent"]), app
    apps = case.get("apps", [])
    return "", apps[0] if apps else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="all")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
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

    output_dir = ROOT / "logs/orchestrator_eval/android" / time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    failed = 0
    for case in cases:
        knowledge, app = _knowledge(case)
        samples = []
        for sample in range(1, max(1, args.k) + 1):
            route = route_message(case["intent"], session=[], platform="android")
            routed_goal = str(route.goal or case["intent"])
            plan = generate_code(
                routed_goal,
                knowledge=knowledge,
                resolution=resolve_intent(routed_goal),
                current_site=app,
            )
            failures = evaluate_source(plan.source, case["contract"])
            if not plan.executable:
                failures.insert(0, "PLAN_NOT_EXECUTABLE")
            failed += bool(failures)
            print(
                f"[{'PASS' if not failures else 'FAIL'}] {case['task_name']} "
                f"sample={sample}/{max(1, args.k)}"
            )
            if failures:
                print("       " + "; ".join(failures[:5]))
            samples.append({
                "sample": sample,
                "ok": not failures,
                "failures": failures,
                "routed_goal": routed_goal,
                "source": plan.source,
                "attempts": [
                    {
                        "diagnostics": [item.render() for item in attempt.diagnostics],
                        "run_ok": attempt.run.ok if attempt.run is not None else None,
                        "run_error": attempt.run.error if attempt.run is not None else "",
                    }
                    for attempt in plan.attempts
                ],
            })
        results.append({**case, "samples": samples})
        (output_dir / "report.json").write_text(
            json.dumps({"cases": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    total = len(cases) * max(1, args.k)
    print(f"\n{total - failed}/{total} samples passed")
    print(f"report -> {output_dir / 'report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
