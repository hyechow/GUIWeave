from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT_ROOT / "plugins" / "guiweave-automation"
PLUGIN_ASSETS = PLUGIN_ROOT / "assets"


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


def test_preview_exposes_three_platforms_without_device_protocol_stacks() -> None:
    source = (PROJECT_ROOT / "gui_agent" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "def run_browser_task(" in source
    assert "def run_android_task(" in source
    assert "def run_iphone_task(" in source
    assert (PROJECT_ROOT / "bin" / "mirror_daemon").is_file()
    assert (PROJECT_ROOT / "bin" / "sck_server").is_file()
    forbidden = ("webdriveragent", "xcuitest", "pymobiledevice", "usbmux")
    assert not any(name in pyproject.lower() for name in forbidden)


def test_plugin_bundles_versioned_device_executables() -> None:
    expected = {
        "iphone-arm64/sck_server": (
            116152,
            "fd6a6af1463315a3619435a9716203951452829b818b404a04e1553d00f487b8",
        ),
        "iphone-arm64/mirror_daemon": (
            178560,
            "63488d5375980174c8a02be13917a89c4c80f0971d21d9e23a28cdaaad41d043",
        ),
        "android-arm64/scrcpy-macos-aarch64-v4.0/adb": (
            19993936,
            "9fdf861259dc807937b13afdd5f053c7fda9f3b7726933fe0e0f45130ecb8dc7",
        ),
        "android-arm64/scrcpy-macos-aarch64-v4.0/scrcpy": (
            8630504,
            "38895166923325d6c1f9d1ba782230e0a5743e9ff7e0b13f319174409bd57b0a",
        ),
        "android-arm64/scrcpy-macos-aarch64-v4.0/scrcpy-server": (
            732226,
            "84924bd564a1eb6089c872c7521f968058977f91f5ff02514a8c74aff3210f3a",
        ),
    }

    for relative, (size, digest) in expected.items():
        path = PLUGIN_ASSETS / relative
        assert path.is_file()
        assert path.stat().st_size == size
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    for relative in (
        "iphone-arm64/sck_server",
        "iphone-arm64/mirror_daemon",
        "android-arm64/scrcpy-macos-aarch64-v4.0/adb",
        "android-arm64/scrcpy-macos-aarch64-v4.0/scrcpy",
    ):
        assert os.access(PLUGIN_ASSETS / relative, os.X_OK)
    assert (PLUGIN_ASSETS / "licenses" / "APACHE-2.0.txt").is_file()
    assert (PLUGIN_ASSETS / "THIRD_PARTY_NOTICES.md").is_file()


def test_plugin_documents_iphone_distribution_signing_gate() -> None:
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Developer ID" in readme
    assert "notarize" in readme
    assert "Gatekeeper" in readme


def test_source_checkout_discovers_the_plugin_bundled_adb() -> None:
    script = (
        "from gui_agent.adapters.android.constants import VENDORED_ADB; "
        "print(VENDORED_ADB)"
    )
    environment = dict(os.environ)
    environment.pop("GUIWEAVE_ADB_PATH", None)

    result = subprocess.run(
        ["uv", "run", "python", "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert Path(result.stdout.strip()) == (
        PLUGIN_ASSETS / "android-arm64/scrcpy-macos-aarch64-v4.0/adb"
    )


def test_mcp_launcher_prefers_bundled_executables(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        '#!/bin/sh\n[ "$1" = "-m" ] && { echo arm64; exit 0; }\nexec /usr/bin/uname "$@"\n',
        encoding="utf-8",
    )
    fake_uname.chmod(0o755)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$GUIWEAVE_ADB_PATH" "$GUIWEAVE_SCRCPY_PATH" '
        '"$GUIWEAVE_SCK_SERVER" "$GUIWEAVE_MIRROR_DAEMON"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    for name in (
        "GUIWEAVE_ADB_PATH",
        "GUIWEAVE_SCRCPY_PATH",
        "GUIWEAVE_SCK_SERVER",
        "GUIWEAVE_MIRROR_DAEMON",
        "ADBUTILS_ADB_PATH",
    ):
        environment.pop(name, None)

    result = subprocess.run(
        [str(PLUGIN_ROOT / "scripts" / "run-mcp")],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == [
        str(PLUGIN_ASSETS / "android-arm64/scrcpy-macos-aarch64-v4.0/adb"),
        str(PLUGIN_ASSETS / "android-arm64/scrcpy-macos-aarch64-v4.0/scrcpy"),
        str(PLUGIN_ASSETS / "iphone-arm64/sck_server"),
        str(PLUGIN_ASSETS / "iphone-arm64/mirror_daemon"),
    ]


def test_source_scrcpy_launcher_skips_arm64_asset_on_intel(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "uname": '#!/bin/sh\necho x86_64\n',
        "sysctl": '#!/bin/sh\necho 0\n',
        "scrcpy": '#!/bin/sh\nexit 0\n',
        "adb": '#!/bin/sh\nexit 0\n',
    }.items():
        executable = fake_bin / name
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        "GUIWEAVE_ADB_PATH": str(fake_bin / "adb"),
    }
    environment.pop("GUIWEAVE_SCRCPY_PATH", None)
    environment.pop("ANDROID_SERIAL", None)

    result = subprocess.run(
        [str(PROJECT_ROOT / "bin" / "scrcpy"), "--width=0"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"scrcpy={fake_bin / 'scrcpy'}" in result.stdout
    assert "plugins/guiweave-automation/assets/android-arm64" not in result.stdout


def test_preview_includes_secret_free_environment_template() -> None:
    template = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "AGENT_CONFIG=config.standard.yaml" in template
    assert "STANDARD_BASE_URL=" in template
    assert "STANDARD_API_KEY=replace-me" in template
    assert "sk-" not in template


def test_sck_server_source_refuses_full_display_capture() -> None:
    source = (PROJECT_ROOT / "sck" / "sck_stream_server.swift").read_text(
        encoding="utf-8"
    )

    assert "refusing full-display capture" in source
    assert "capturing full display" not in source
