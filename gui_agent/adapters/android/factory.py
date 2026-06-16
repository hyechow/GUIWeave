"""Android adapter factory: the one place android construction is wired together.

Builds a :class:`gui_agent.core.runtime.factory.PlatformBundle` whose callables construct
the android session (phone over adb), executor, perception, action policy and
supervisor. Core orchestration receives the neutral bundle and never imports these
classes directly. Mirrors ``adapters/browser/factory.py``.

SCROLL-COLLECT NOT YET SUPPORTED
--------------------------------
The iphone-shaped scroll/stitch bundle fields back the runner's scroll-collect /
stitching branch, which the android adapter does NOT implement yet:
  - ``apply_scroll_profile`` is the identity (no per-platform scroll profiles).
  - ``make_scroll_probe`` / ``make_stitch_accumulator`` / ``robust_shift`` /
    ``gray_u8`` raise ``NotImplementedError``.
That branch is reached when the milestone supervisor plans
completion_strategy='scroll_until_boundary' (e.g. "read all items on this page"),
so such a goal raises mid-run. Until android collection is built, restrict the
android platform to direct-action goals.
The status reporter (HUD) is None: android has no on-screen agent HUD yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from gui_agent.core.runtime.factory import PlatformBundle, SetupCheckResult

if TYPE_CHECKING:
    from gui_agent.core.runtime.contracts import ActionPolicy, SupervisorPolicy


# Registries (mirror the browser adapter shape). Android is vision-only with a
# single action policy today; the default supervisor is structure-neutral milestone.
_POLICY_NAMES: tuple[str, ...] = ("android_vision",)
_SUPERVISOR_NAMES: tuple[str, ...] = ("milestone",)


def _build_action_policy(name: str) -> "ActionPolicy":
    from gui_agent.adapters.android.policies import AndroidActionPolicy

    registry: dict[str, type] = {AndroidActionPolicy.name: AndroidActionPolicy}
    try:
        return registry[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"未知策略 {name!r}，可选：{choices}") from exc


def _build_supervisor(name: str) -> "SupervisorPolicy":
    # Android uses the structure-neutral milestone supervisor FRAMEWORK with its OWN
    # mobile-tuned prompts injected (no longer the iphone-flavored default that called
    # everything an "iOS 主屏"). The iphone SimpleSupervisorPolicy is not offered here.
    from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
    from gui_agent.adapters.android.supervisor.milestone.prompts import ANDROID_MILESTONE_PROMPTS

    if name == MilestoneSupervisorPolicy.name:
        return MilestoneSupervisorPolicy(prompts=ANDROID_MILESTONE_PROMPTS)
    raise ValueError(f"未知监督者 {name!r}，可选：{MilestoneSupervisorPolicy.name}")


def _apply_scroll_profile(action: object, profile: object) -> object:
    """Identity: android has no per-platform scroll profiles (no-op pass-through)."""
    return action


_SCROLL_COLLECT_MSG = (
    "android scroll-collect not yet supported (the milestone supervisor planned "
    "completion_strategy='scroll_until_boundary'). Use a direct-action goal. See the "
    "SCROLL-COLLECT note in adapters/android/factory.py."
)


def _make_scroll_probe(session: object, executor: object, log_dir: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (android collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _make_stitch_accumulator(*args: object, **kwargs: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (android collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _robust_shift(*args: object, **kwargs: object) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (android collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


def _gray_u8(png_bytes: bytes) -> object:
    """Raised if a scroll_until_boundary milestone reaches the runner's collection branch (android collection not yet supported)."""
    raise NotImplementedError(_SCROLL_COLLECT_MSG)


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


def _make_android_hud() -> object:
    """Status HUD positioned just BELOW the scrcpy mirror window (the iphone model —
    a narrow phone mirror with the HUD under it, NOT browser's over-the-wide-window
    placement). The neutral core ``AgentHUD`` draws its own OS window, so it never
    enters the agent's adb screenshot. Falls back to a default screen spot when no
    scrcpy window is on screen (agent runs headless of any mirror)."""
    from gui_agent.core.ui.hud import AgentHUD
    from gui_agent.adapters.android.visualizer import scrcpy_window_rect

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
    from gui_agent.adapters.android.executor import AndroidExecutor
    from gui_agent.adapters.android.perception import AndroidPerception, AndroidSession

    return PlatformBundle(
        platform="android",
        open_session=lambda: AndroidSession(serial=serial),
        setup_check=lambda: _setup_check(serial),
        make_executor=lambda session: AndroidExecutor(session),
        make_perception=lambda session, png_path: AndroidPerception(session, png_path),
        make_action_policy=_build_action_policy,
        make_supervisor=_build_supervisor,
        make_status_reporter=lambda enabled: (_make_android_hud() if enabled else None),
        make_action_visualizer=_make_action_visualizer,
        make_scroll_probe=_make_scroll_probe,
        apply_scroll_profile=_apply_scroll_profile,
        make_stitch_accumulator=_make_stitch_accumulator,
        robust_shift=_robust_shift,
        gray_u8=_gray_u8,
        default_action_policy="android_vision",
        default_supervisor="milestone",
        action_policy_choices=_POLICY_NAMES,
        supervisor_choices=_SUPERVISOR_NAMES,
    )
