"""Run a named collection of production decision replays.

Each case invokes the normal ``python -m replay`` entry point in a fresh process.
The process boundary prevents model/runtime state from leaking between cases and
keeps a suite failure equivalent to running that case by hand.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _case_command(case: dict[str, Any]) -> list[str]:
    run_dir = Path(str(case["run_dir"]))
    if not run_dir.is_absolute():
        run_dir = PROJECT_ROOT / run_dir
    executor = str(case.get("executor") or "transition")
    command = [
        sys.executable,
        "-m",
        "replay.read" if executor == "read" else "replay",
        str(run_dir),
        "--turn",
        str(case["turn"]),
    ]
    if executor == "read":
        command.extend([
            "--request-json",
            json.dumps(
                case["request"],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ])
    elif executor != "transition":
        raise ValueError(f"unsupported replay executor: {executor!r}")
    command.extend([
        "--expect-json",
        json.dumps(case["expectation"], ensure_ascii=False, separators=(",", ":")),
    ])
    if statement_id := str(case.get("statement_id") or "").strip():
        command.extend(["--statement-id", statement_id])
    if case.get("with_action_policy"):
        command.append("--with-action-policy")
    return command


def run_suite(
    suite_path: Path,
    *,
    process_runner=None,
) -> dict[str, Any]:
    """Execute every case and return a compact summary."""
    suite_path = suite_path.resolve()
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    if suite.get("schema_version") != 1 or not suite.get("cases"):
        raise ValueError("replay suite requires schema_version=1 and non-empty cases")
    process_runner = process_runner or subprocess.run
    cases = suite["cases"]
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for case in cases:
        case_started = time.perf_counter()
        completed = process_runner(
            _case_command(case),
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        passed = completed.returncode == 0
        result = {
            "name": case["name"],
            "passed": passed,
            "returncode": completed.returncode,
            "elapsed_s": round(time.perf_counter() - case_started, 3),
        }
        results.append(result)
        print(f"[{'PASS' if passed else 'FAIL'}] {case['name']} ({result['elapsed_s']:.3f}s)")
        if not passed:
            if completed.stdout.strip():
                print(completed.stdout.rstrip())
            if completed.stderr.strip():
                print(completed.stderr.rstrip(), file=sys.stderr)

    passed_count = sum(result["passed"] for result in results)
    return {
        "suite": suite.get("name") or suite_path.stem,
        "passed": passed_count == len(cases),
        "passed_count": passed_count,
        "case_count": len(cases),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a manifest of replay regressions through the production replay CLI."
    )
    parser.add_argument("suite", type=Path, help="replay suite JSON manifest")
    args = parser.parse_args()

    result = run_suite(args.suite)
    print(
        f"[SUMMARY] {result['passed_count']}/{result['case_count']} passed "
        f"in {result['elapsed_s']:.3f}s"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
