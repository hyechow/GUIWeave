from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins" / "guiweave-automation"


def test_bundled_mcp_config_uses_supported_server_map() -> None:
    manifest = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    servers = manifest["mcpServers"]
    assert set(servers) == {"guiweave-automation"}
    assert servers["guiweave-automation"]["command"] == "bash"
    assert servers["guiweave-automation"]["args"] == ["./scripts/run-mcp"]


def test_installed_mcp_launcher_resolves_local_marketplace_checkout(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_uv.chmod(0o755)

    installed_scripts = (
        tmp_path
        / "codex-home"
        / "plugins"
        / "cache"
        / "guiweave-dev"
        / "guiweave-automation"
        / "0.1.0"
        / "scripts"
    )
    installed_scripts.mkdir(parents=True)
    launcher = installed_scripts / "run-mcp"
    shutil.copy2(PLUGIN_ROOT / "scripts" / "run-mcp", launcher)
    codex_home = tmp_path / "codex-home"
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                "[marketplaces.guiweave-dev]",
                'source_type = "local"',
                f'source = "{PROJECT_ROOT}"',
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(launcher)],
        cwd=tmp_path,
        env={
            **os.environ,
            "CODEX_HOME": str(codex_home),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        "run",
        "--project",
        str(PROJECT_ROOT),
        "guiweave-mcp",
    ]


def test_preview_scripts_do_not_reference_removed_runner() -> None:
    launcher = (PROJECT_ROOT / "bin" / "launch_chrome_cdp").read_text(
        encoding="utf-8"
    )

    assert "gui_agent.core.runner" not in launcher
    assert "uv run guiweave run browser" in launcher
    assert os.access(PROJECT_ROOT / "bin" / "build_agent_cursor", os.X_OK)


def test_mcp_server_exposes_knowledge_import_tools() -> None:
    source = (PROJECT_ROOT / "gui_agent" / "mcp_server.py").read_text(encoding="utf-8")

    for tool_name in (
        "preview_knowledge_document",
        "get_knowledge_draft",
        "commit_knowledge_draft",
        "list_user_knowledge",
        "get_user_knowledge",
    ):
        assert f"def {tool_name}(" in source
