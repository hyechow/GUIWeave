"""Android adapter factory: the one place android construction is wired together.

Builds a :class:`gui_agent.core.runtime.factory.PlatformBundle` whose callables construct
the android session (phone over adb), executor, perception, action policy and
supervisor. Core orchestration receives the neutral bundle and never imports these
classes directly. Mirrors ``adapters/browser/factory.py``.

OBSERVATION MODEL
-----------------
The current frame is the complete visual input. Semantic Interact execution still
supports ordinary scrolling and per-frame reading.
The status reporter (HUD) is None: android has no on-screen agent HUD yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult

if TYPE_CHECKING:
    from gui_agent.core.runtime.contracts import ActionPolicy, SupervisorPolicy


# Registries (mirror the browser adapter shape). Android is vision-only with a
# single action policy today; the default supervisor is structure-neutral statement.
_POLICY_NAMES: tuple[str, ...] = ("android_vision",)
_SUPERVISOR_NAMES: tuple[str, ...] = ("statement",)


def _build_action_policy(name: str) -> "ActionPolicy":
    from gui_agent.adapters.android.policies import AndroidActionPolicy

    registry: dict[str, type] = {AndroidActionPolicy.name: AndroidActionPolicy}
    try:
        return registry[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc


def _build_supervisor(name: str) -> "SupervisorPolicy":
    # Android uses the structure-neutral statement supervisor FRAMEWORK with its OWN
    # mobile-tuned prompts injected (no longer the iphone-flavored default that called
    # everything an "iOS 主屏").
    from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
    from gui_agent.adapters.android.supervisor.statement.prompts import ANDROID_STATEMENT_PROMPTS

    if name == StatementSupervisorPolicy.name:
        return StatementSupervisorPolicy(prompts=ANDROID_STATEMENT_PROMPTS)
    raise ValueError(f"未知监督者 {name!r}，可选：{StatementSupervisorPolicy.name}")


def _prepare_vision_prompt_png(png_bytes: bytes) -> bytes:
    return png_bytes


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
    import shutil

    from gui_agent.adapters.android.constants import VENDORED_ADB
    from gui_agent.adapters.android.device import AndroidDevice

    dev = AndroidDevice(serial=serial)
    try:
        dev.connect()
    except Exception as exc:  # noqa: BLE001
        return SetupCheckResult(
            ok=False,
            summary="adb 设备不可用",
            lines=(
                f"  ✗ 连不上 adb 设备：{exc}",
                "    设 ANDROID_SERIAL，或 `adb connect <ip:port>`（无线）/ 插 USB",
            ),
        )
    lines = [f"  ✓ adb 设备已连接 ({dev.win_w}x{dev.win_h})"]
    try:
        scrcpy_ok = (VENDORED_ADB.parent / "scrcpy").exists() or bool(shutil.which("scrcpy"))
        lines.append(
            "  ✓ scrcpy 可用（镜像窗口 / 动作可视化）"
            if scrcpy_ok
            else "  ⚠ 未找到 scrcpy——镜像窗口 / 动作可视化不可用（agent 仍可无镜像运行）"
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
        script = VENDORED_ADB.parents[2] / "bin" / "scrcpy"  # repo_root/bin/scrcpy
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
    """Status HUD positioned just BELOW the scrcpy mirror window (the iphone model —
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
    single action path — unlike iphone, which drives at the device layer."""
    from gui_agent.adapters.android.visualizer import AndroidActionVisualizer

    return AndroidActionVisualizer(session)


def build_android_bundle(
    *,
    backend: Optional[str] = None,
    serial: Optional[str] = None,
    **_ignored: object,
) -> PlatformBundle:
    """Construct the android PlatformBundle.

    ``backend`` is accepted for signature parity with the iphone factory (no android
    backends today). ``serial`` flows through to the session (else the session falls
    back to env ``ANDROID_SERIAL``, else auto-selects the sole adb device).
    """
    from gui_agent.adapters.android.acquisition import move_collection
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
        make_action_policy=_build_action_policy,
        make_supervisor=_build_supervisor,
        make_status_reporter=lambda enabled: (_make_android_hud() if enabled else None),
        make_action_visualizer=_make_action_visualizer,
        prepare_vision_prompt_png=_prepare_vision_prompt_png,
        move_collection=move_collection,
        validate_collection_action=None,
        default_action_policy="android_vision",
        default_supervisor="statement",
        action_policy_choices=_POLICY_NAMES,
        supervisor_choices=_SUPERVISOR_NAMES,
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
