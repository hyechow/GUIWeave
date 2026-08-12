from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gui_agent.adapters.browser.webarena import _build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _wrapper_args(tmp_path: Path, *extra: str) -> list[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text("[]", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "TASKS_FILE": str(tasks_file),
        "TASK_OUTPUT_ROOT": str(tmp_path / "output"),
        "RUN_DIR": "test_run",
    }
    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "webarena"), "549", *extra],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_wrapper_preserves_user_max_turns(tmp_path: Path) -> None:
    args = _wrapper_args(tmp_path, "--max-turns", "40", "--confirm")

    assert args.count("--max-turns") == 1
    index = args.index("--max-turns")
    assert args[index + 1] == "40"
    assert "--confirm" in args


def test_wrapper_leaves_default_max_turns_to_python_cli(tmp_path: Path) -> None:
    args = _wrapper_args(tmp_path)

    assert "--max-turns" not in args


def test_tool_agent_runtime_receives_python_cli_max_turns() -> None:
    source = (
        PROJECT_ROOT / "gui_agent" / "adapters" / "browser" / "webarena.py"
    ).read_text(encoding="utf-8")

    assert "max_turns=args.max_turns" in source


def test_python_cli_uses_runtime_specific_default_max_turns() -> None:
    source = (
        PROJECT_ROOT / "gui_agent" / "adapters" / "browser" / "webarena.py"
    ).read_text(encoding="utf-8")

    assert '_REVIEWED_PYTHON_MAX_TURNS = 25' in source
    assert '_TOOL_AGENT_MAX_TURNS = 50' in source
    assert '_MAX_TURNS = 50' in source
    assert 'default=None' in source
    assert 'if args.runtime == "tool-agent"' in source


def test_webarena_enables_tool_agent_multi_action_by_default() -> None:
    parser = _build_parser()

    assert parser.parse_args([]).tool_agent_multi_action is True
    assert parser.parse_args([
        "--no-tool-agent-multi-action",
    ]).tool_agent_multi_action is False
