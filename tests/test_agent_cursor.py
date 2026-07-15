from __future__ import annotations

from pathlib import Path

from sck.agent_cursor import cursor_bin_candidates, ensure_cursor_bin


def test_missing_cursor_binary_disables_visualization_without_compiling(
    monkeypatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "agent_cursor"
    monkeypatch.setenv("AGENT_CURSOR_BIN", str(missing))

    assert ensure_cursor_bin() is None
    assert not missing.exists()


def test_existing_executable_cursor_binary_is_reused(
    monkeypatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "agent_cursor"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("AGENT_CURSOR_BIN", str(binary))

    assert ensure_cursor_bin() == str(binary)


def test_default_cursor_binary_uses_durable_user_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AGENT_CURSOR_BIN", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    binary = tmp_path / "guiweave" / "bin" / "agent_cursor"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    assert cursor_bin_candidates()[0] == binary
    assert ensure_cursor_bin() == str(binary)
