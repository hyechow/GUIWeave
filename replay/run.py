"""Replay a Tool Agent run without a browser, device, network, or LLM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gui_agent.core.tool_agent.replay import load_recorded_run, replay_recorded_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    run_dir = args.run_dir.expanduser().resolve()
    result = replay_recorded_run(load_recorded_run(run_dir))
    payload = result.to_dict()
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[{result.status.upper()}] {result.summary}")
        print(f"phase={result.phase} programs={result.program_count} workers={result.gui_worker_count}")
        if result.error:
            print(f"error={result.error}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
