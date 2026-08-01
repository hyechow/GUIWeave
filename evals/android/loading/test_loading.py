"""Loading perception eval: accuracy, tier routing, and VLM fallback latency.

Run:
  uv run python evals/android/loading/test_loading.py
  uv run python evals/android/loading/test_loading.py --group splash
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from gui_agent.core.schemas import Observation  # noqa: E402
from gui_agent.core.vision.loading import (  # noqa: E402
    assess_loading,
    heuristic_loading_assessment,
)

CASES_FILE = Path(__file__).with_name("cases.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="all")
    args = parser.parse_args()
    cases = [
        case
        for case in json.loads(CASES_FILE.read_text(encoding="utf-8"))
        if args.group == "all" or case["group"] == args.group
    ]
    passed = 0
    fallback_count = 0
    fallback_seconds = 0.0
    for case in cases:
        screenshot = ROOT / case["screenshot"]
        if not screenshot.exists():
            print(f"[FAIL] {case['label']}: missing {case['screenshot']}")
            continue
        payload = {}
        if case.get("observation"):
            observation_path = ROOT / case["observation"]
            if not observation_path.exists():
                print(f"[FAIL] {case['label']}: missing {case['observation']}")
                continue
            payload = json.loads(observation_path.read_text(encoding="utf-8"))
            payload = payload.get("observation", payload)
        observation = Observation.model_validate({
            **payload,
            "png_bytes": screenshot.read_bytes(),
            "source": "android-eval",
        })
        heuristic = heuristic_loading_assessment(observation)
        started = time.monotonic()
        result = assess_loading(observation)
        elapsed = time.monotonic() - started
        used_fallback = heuristic.state == "uncertain"
        if used_fallback:
            fallback_count += 1
            fallback_seconds += elapsed
        expected_route = case["expected_route"]
        route = "vlm" if used_fallback else result.source
        ok = result.state == case["expected"] and route == expected_route
        passed += ok
        print(
            f"[{'PASS' if ok else 'FAIL'}] {case['label']}: "
            f"state={result.state} route={route} time={elapsed:.2f}s; {result.reason}"
        )

    total = len(cases)
    accuracy = passed / total if total else 0.0
    average_fallback = fallback_seconds / fallback_count if fallback_count else 0.0
    print(
        f"\naccuracy={passed}/{total} ({accuracy:.1%}), "
        f"vlm_fallback={fallback_count}/{total}, "
        f"avg_vlm_latency={average_fallback:.2f}s"
    )
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
