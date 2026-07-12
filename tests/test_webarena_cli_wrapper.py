from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
