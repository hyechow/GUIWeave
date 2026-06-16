"""Compatibility entrypoint for the agent loop runner."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gui_agent.core.run.io import create_run_dir
from gui_agent.core.run.loop import build_policy, build_supervisor, run_agent_loop

__all__ = [
    "build_policy",
    "build_supervisor",
    "create_run_dir",
    "run_agent_loop",
]


def main() -> None:
    from gui_agent.core.run.cli import main as cli_main

    cli_main(
        run_loop=run_agent_loop,
        policy_builder=build_policy,
        supervisor_builder=build_supervisor,
    )


if __name__ == "__main__":
    main()
