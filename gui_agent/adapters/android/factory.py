"""Android adapter factory: the one place android construction is wired together.

Builds a :class:`gui_agent.core.runtime.factory.PlatformBundle` whose callables construct
the Android session (phone over adb), executor and perception for Tool Agent.

OBSERVATION MODEL
-----------------
The current frame is the complete visual input. Semantic Interact execution still
supports ordinary scrolling and per-frame reading.
The status reporter (HUD) is None: android has no on-screen agent HUD yet.
"""

from __future__ import annotations

import platform as platform_module
import subprocess
from typing import Optional

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult


def _is_apple_silicon() -> bool:
    """Return true on M-series hardware, including a process under Rosetta."""

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


def _setup_check(serial: "Optional[str]") -> SetupCheckResult:
    """Pre-session environment check for android:
      1. an adb device is reachable (HARD block — nothing works without it);
      2. scrcpy is available (WARN only — it backs the optional mirror window /
         HUD / cursor visualization; the agent runs headless of it via adb);
      3. ADBKeyboard is installed → switch the device IME to it now (Chinese-input
         setup; WARN if absent — ASCII input is unaffected).
    The IME switch lives HERE, in setup (before the session opens, before any field is
    focused), not in the per-connect path. Uses a short-lived adb connection; the switch
    persists device-side, so the real session's connect() then detects ADBKeyboard."""
    import os
    import shutil
    from pathlib import Path

    from gui_agent.adapters.android.constants import VENDORED_ADB
    from gui_agent.adapters.android.device import AndroidDevice

    configured_adb = os.environ.get("ADBUTILS_ADB_PATH", "").strip()
    candidates = (
        Path(configured_adb).expanduser() if configured_adb else None,
        VENDORED_ADB,
        Path(system_adb) if (system_adb := shutil.which("adb")) else None,
    )
    adb_path = next(
        (
            candidate
            for candidate in candidates
            if candidate is not None
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ),
        None,
    )
    if adb_path is None:
        return SetupCheckResult(
            ok=False,
            summary="adb 可执行文件不可用",
            lines=(
                "  ✗ 插件内未找到可执行 adb，PATH 中也没有 adb",
                "    重新安装完整插件，或安装 Android Platform Tools",
            ),
        )

    lines = [f"  ✓ adb 可执行文件: {adb_path}"]
    dev = AndroidDevice(serial=serial)
    try:
        dev.connect()
    except Exception as exc:  # noqa: BLE001
        return SetupCheckResult(
            ok=False,
            summary="adb 设备不可用",
            lines=(
                *lines,
                f"  ✗ 连不上 adb 设备：{exc}",
                "    设 ANDROID_SERIAL，或 `adb connect <ip:port>`（无线）/ 插 USB",
            ),
        )
    lines.append(f"  ✓ adb 设备已连接 ({dev.win_w}x{dev.win_h})")
    serial_label = dev.serial or serial or "<device>"
    if dev.stay_awake_enabled:
        lines.append("  ✓ 已启用充电时保持亮屏（svc power stayon true）")
    else:
        lines.append("  ⚠ 无法启用充电时保持亮屏——深度休眠可能断开无线 adb")
    lines.extend((
        "  ! stayon 不会解除锁屏；请关闭自动锁屏或延长休眠时间",
        f"    恢复默认：adb -s {serial_label} shell svc power stayon false",
    ))
    try:
        configured_scrcpy = os.environ.get("GUIWEAVE_SCRCPY_PATH", "").strip()
        configured_scrcpy_path = (
            Path(configured_scrcpy).expanduser() if configured_scrcpy else None
        )
        bundled_scrcpy = VENDORED_ADB.parent / "scrcpy"
        bundled_scrcpy_exists = (
            bundled_scrcpy.is_file() and os.access(bundled_scrcpy, os.X_OK)
        )
        apple_silicon = _is_apple_silicon()
        system_scrcpy = shutil.which("scrcpy")
        scrcpy_path = next(
            (
                candidate
                for candidate in (
                    configured_scrcpy_path,
                    bundled_scrcpy if apple_silicon else None,
                    Path(system_scrcpy) if system_scrcpy else None,
                )
                if candidate is not None
                and candidate.is_file()
                and os.access(candidate, os.X_OK)
            ),
            None,
        )
        if scrcpy_path is not None:
            lines.append(f"  ✓ scrcpy 可用（镜像窗口 / 动作可视化）：{scrcpy_path}")
        elif bundled_scrcpy_exists and not apple_silicon:
            lines.append(
                "  ⚠ 插件内 scrcpy 仅支持 arm64；Intel Mac 请安装兼容版本到 PATH"
                "（agent 仍可无镜像运行）"
            )
        else:
            lines.append(
                "  ⚠ 未找到 scrcpy——镜像窗口 / 动作可视化不可用"
                "（agent 仍可无镜像运行）"
            )
        installed, active = dev.ensure_adbkeyboard()
        if active:
            lines.append("  ✓ ADBKeyboard 已设为输入法（支持中文、无软键盘遮挡）")
        elif installed:
            lines.append("  ⚠ ADBKeyboard 已安装但切换失败——中文输入可能不可用")
        else:
            lines.append("  ⚠ 未安装 ADBKeyboard——中文输入不可用（ASCII 正常）")
    finally:
        dev.close()
    return SetupCheckResult(ok=True, summary="android 环境就绪", lines=tuple(lines))


def _scrcpy_running(serial: str) -> bool:
    """True if a scrcpy CLIENT process for ``serial`` (or any, when serial is empty) is already
    running — the robust idempotency guard. Process-based, not window-based: scrcpy exits when its
    window closes, so a live client ⟺ a live mirror, and this avoids the window-detection gap
    (CGWindowList can momentarily return None during teardown churn → spurious relaunch)."""
    import subprocess

    try:
        out = subprocess.run(["pgrep", "-fl", "scrcpy"], capture_output=True, text=True).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if "/adb " in line or " shell " in line or "scrcpy-server" in line:
            continue  # device-side server / adb helper, not the desktop client
        if "/scrcpy " not in line and not line.rstrip().endswith("/scrcpy"):
            continue  # not the scrcpy client binary
        if not serial or f"-s {serial}" in line:
            return True
    return False


def _ensure_scrcpy_window(timeout_s: float = 6.0) -> None:
    """Best-effort: open the scrcpy mirror window (background) for the current device if one is
    not already up, then wait briefly for it to appear. Interactive (non-headless) runs want the
    mirror — the HUD and the action-cursor overlay draw on it. No-op if scrcpy is already running
    for this device (or a mirror is already on screen), if bin/scrcpy can't be launched, or
    off-macOS — the agent runs fine without a mirror. Reuses bin/scrcpy."""
    import os
    import subprocess
    import time

    from gui_agent.adapters.android.constants import VENDORED_ADB
    from gui_agent.adapters.android.visualizer import scrcpy_window_rect

    try:
        serial = (os.environ.get("ANDROID_SERIAL") or "").strip()
        if _scrcpy_running(serial) or scrcpy_window_rect() is not None:
            return  # already mirroring — never relaunch
        script = Path(__file__).resolve().parents[3] / "bin" / "scrcpy"
        if not script.exists():
            return
        # No serial arg: bin/scrcpy reads ANDROID_SERIAL from the inherited env (works for both
        # USB serials and host:port wireless; passing a non-colon USB serial as arg would break it).
        subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if scrcpy_window_rect() is not None:
                return
            time.sleep(0.3)
    except Exception:
        pass


def _make_android_hud() -> object:
    """Status HUD positioned just below the narrow scrcpy mirror window —
    a narrow phone mirror with the HUD under it, NOT browser's over-the-wide-window
    placement). The neutral core ``AgentHUD`` draws its own OS window, so it never
    enters the agent's adb screenshot. Auto-opens the scrcpy mirror first (interactive
    runs only — this path is gated by make_status_reporter(not headless)); falls back to
    a default screen spot if the mirror can't be opened."""
    from gui_agent.core.ui.hud import AgentHUD
    from gui_agent.adapters.android.visualizer import scrcpy_window_rect

    _ensure_scrcpy_window()
    rect = scrcpy_window_rect()
    if rect:
        wx, wy, ww, wh = rect
        hud_w = max(ww, 318)
        hud_x = wx + (ww - hud_w) // 2     # center under the (narrow) mirror
        hud_y = wy + wh + 2                # just below the scrcpy window
    else:
        hud_x, hud_y, hud_w = 100, 100, 318
    return AgentHUD(origin=(hud_x, hud_y), width=hud_w, height=150, alpha=1.0)


def _make_action_visualizer(session: object) -> object:
    """The android ActionVisualizer: the shared agent_cursor blue arrow glided over
    the scrcpy window (see adapters/android/visualizer.py). Runner-driven (pure
    visualization, decoupled from perception) because android has no snapping and a
    single action path."""
    from gui_agent.adapters.android.visualizer import AndroidActionVisualizer

    return AndroidActionVisualizer(session)


def build_android_bundle(
    *,
    backend: Optional[str] = None,
    serial: Optional[str] = None,
    **_ignored: object,
) -> PlatformBundle:
    """Construct the android PlatformBundle.

    ``backend`` is reserved for future adapter backends. ``serial`` flows through to the session (else the session falls
    back to env ``ANDROID_SERIAL``, else auto-selects the sole adb device).
    """
    from gui_agent.adapters.android.actions import AndroidAction
    from gui_agent.adapters.android.executor import AndroidExecutor
    from gui_agent.adapters.android.perception import AndroidPerception, AndroidSession

    return PlatformBundle(
        platform="android",
        open_session=lambda: AndroidSession(serial=serial),
        setup_check=lambda: _setup_check(serial),
        make_executor=lambda session: AndroidExecutor(session),
        make_action=lambda payload: AndroidAction.model_validate(payload),
        make_perception=lambda session, png_path: AndroidPerception(session, png_path),
        make_status_reporter=lambda enabled: (_make_android_hud() if enabled else None),
        make_action_visualizer=_make_action_visualizer,
        read_time=lambda session: session.platform_time(),
        tool_agent_capabilities=(
            "tap",
            "type",
            "clear_text",
            "press_enter",
            "scroll",
            "drag",
            "long_press",
            "back",
            "home",
            "app_switch",
            "launch_app",
        ),
    )
