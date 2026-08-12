"""Construct the iPhone Tool Agent platform bundle."""

from __future__ import annotations

import os
import platform as platform_module
import subprocess
import sys
from pathlib import Path

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult


def _is_apple_silicon() -> bool:
    """Return true for M-series hardware, including Python running under Rosetta."""

    if platform_module.machine().lower() in {"arm64", "aarch64"}:
        return True
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def _display_helper_path(path: Path) -> str:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        return str(path.relative_to(repository_root))
    except ValueError:
        return str(path)


def _gatekeeper_assessment(path: Path) -> tuple[str, str]:
    """Assess whether a helper will survive macOS download quarantine.

    Ad-hoc signed helpers can run from a local checkout, but Gatekeeper rejects
    them once a downloaded archive adds ``com.apple.quarantine``.  Keep that
    source-preview path explicit while hard-blocking a quarantined helper that
    macOS will refuse to execute.
    """

    try:
        assessment = subprocess.run(
            ["spctl", "--assess", "--type", "execute", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return (
            "unavailable",
            "无法调用 spctl 验证 Gatekeeper 状态",
        )
    if assessment.returncode == 0:
        return ("accepted", "已通过 Gatekeeper 执行评估")

    try:
        quarantine = subprocess.run(
            ["xattr", "-p", "com.apple.quarantine", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        quarantine = None
    if quarantine is not None and quarantine.returncode == 0:
        return (
            "blocked",
            "helper 带下载隔离标记且未通过 Gatekeeper；请使用 Developer ID 签名并公证的发布包",
        )
    return (
        "preview",
        "仅适合本地源码预览；对外发布前需 Developer ID 签名并完成 notarization",
    )


def mirroring_window_bounds() -> tuple[float, float, float, float] | None:
    try:
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
        )
        for window in windows or []:
            owner = str(window.get("kCGWindowOwnerName", "")).lower()
            if "iphone" not in owner:
                continue
            bounds = window.get("kCGWindowBounds", {})
            width = float(bounds.get("Width", 0))
            height = float(bounds.get("Height", 0))
            if width > 100 and height > 400:
                return (
                    float(bounds.get("X", 0)),
                    float(bounds.get("Y", 0)),
                    width,
                    height,
                )
    except Exception:
        return None
    return None


def _setup_check() -> SetupCheckResult:
    from gui_agent.adapters.iphone.client import MIRROR_DAEMON, SCK_SERVER

    lines: list[str] = []
    if sys.platform != "darwin":
        return SetupCheckResult(
            ok=False,
            summary="iPhone 平台仅支持 macOS",
            lines=("  ✗ 需要 macOS 的 iPhone 镜像应用",),
        )
    if not _is_apple_silicon():
        return SetupCheckResult(
            ok=False,
            summary="iPhone helper 仅支持 Apple Silicon Mac",
            lines=("  ✗ 当前开发者预览版仅支持搭载 M 系列芯片的 Mac",),
        )
    helpers = (MIRROR_DAEMON, SCK_SERVER)
    missing = [path for path in helpers if not path.is_file()]
    not_executable = [
        path for path in helpers if path.is_file() and not os.access(path, os.X_OK)
    ]
    gatekeeper_blocked: list[Path] = []
    for path in helpers:
        display_path = _display_helper_path(path)
        if not path.is_file():
            lines.append(f"  ✗ {display_path} 不存在")
        elif not os.access(path, os.X_OK):
            lines.append(f"  ✗ {display_path} 不可执行（运行 chmod +x {display_path}）")
        else:
            lines.append(f"  ✓ {display_path} 可执行")
            status, detail = _gatekeeper_assessment(path)
            if status == "accepted":
                lines.append(f"    ✓ {detail}")
            elif status == "blocked":
                gatekeeper_blocked.append(path)
                lines.append(f"    ✗ {detail}")
            else:
                lines.append(f"    ⚠ {detail}")
    rect = mirroring_window_bounds()
    if rect is None:
        lines.append("  ✗ 未找到 iPhone 镜像窗口，请先打开并保持窗口可见")
    else:
        lines.append(f"  ✓ iPhone 镜像窗口 ({int(rect[2])}x{int(rect[3])})")
    if missing or not_executable:
        return SetupCheckResult(
            ok=False,
            summary="iPhone 本地 helper 不完整或不可执行",
            lines=tuple(lines),
        )
    if gatekeeper_blocked:
        return SetupCheckResult(
            ok=False,
            summary="iPhone helper 被 macOS Gatekeeper 阻止",
            lines=tuple(lines),
        )
    if rect is None:
        return SetupCheckResult(
            ok=False,
            summary="iPhone 镜像窗口未打开",
            lines=tuple(lines),
        )
    return SetupCheckResult(ok=True, summary="iPhone 环境就绪", lines=tuple(lines))


def build_iphone_bundle(
    *,
    backend: str | None = None,
    **_ignored: object,
) -> PlatformBundle:
    from gui_agent.adapters.iphone.actions import IPhoneAction
    from gui_agent.adapters.iphone.executor import IPhoneExecutor
    from gui_agent.adapters.iphone.perception import IPhonePerception, IPhoneSession

    if backend is not None:
        raise ValueError(
            "iPhone backend is fixed: sck_server screenshots and mirror_daemon input"
        )
    return PlatformBundle(
        platform="iphone",
        open_session=IPhoneSession,
        setup_check=_setup_check,
        make_executor=lambda session: IPhoneExecutor(session),
        make_action=lambda payload: IPhoneAction.model_validate(payload),
        make_perception=lambda session, png_path: IPhonePerception(session, png_path),
        make_status_reporter=lambda enabled: None,
        make_action_visualizer=lambda session: None,
        read_time=lambda session: session.platform_time(),
        tool_agent_capabilities=(
            "tap",
            "type",
            "clear_text",
            "press_enter",
            "scroll",
            "drag",
            "home",
            "app_switch",
        ),
    )


__all__ = ["build_iphone_bundle", "mirroring_window_bounds"]
