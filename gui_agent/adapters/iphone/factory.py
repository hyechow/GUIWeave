"""Construct the iPhone Tool Agent platform bundle."""

from __future__ import annotations

import sys
from pathlib import Path
from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult


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
    missing = [path for path in (MIRROR_DAEMON, SCK_SERVER) if not path.is_file()]
    for path in (MIRROR_DAEMON, SCK_SERVER):
        marker = "✓" if path.is_file() else "✗"
        lines.append(f"  {marker} {path.relative_to(Path(__file__).resolve().parents[3])}")
    rect = mirroring_window_bounds()
    if rect is None:
        lines.append("  ✗ 未找到 iPhone 镜像窗口，请先打开并保持窗口可见")
    else:
        lines.append(f"  ✓ iPhone 镜像窗口 ({int(rect[2])}x{int(rect[3])})")
    if missing:
        return SetupCheckResult(
            ok=False,
            summary="iPhone 本地 helper 不完整",
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
