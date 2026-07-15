"""Agent 虚拟光标客户端 —— 驱动 sck/agent_cursor.swift 守护进程。

项目自有的 agent 光标可视化:一个透明、点击穿透、不抢焦点的 overlay,蓝渐变箭头 + bloom,
跨点平滑滑动。click/scroll/drag 在动作前调用 move(),全局只有这一个 agent 光标;真实物理
光标不动。坐标 = 逻辑屏幕点、左上原点(同 CGWindow 全局坐标)。

独立 overlay 进程(不整合进 mirror_daemon,避免浮层进截图污染 OCR/YOLO)。
二进制默认安装到用户缓存目录 ~/.cache/guiweave/bin/agent_cursor,可用环境变量
AGENT_CURSOR_BIN 覆盖。运行时只加载已有二进制,不在 action 路径编译。
  build: bin/build_agent_cursor

用法:
    cur = AgentCursor(); cur.start()
    cur.move(sx, sy)      # 动作前把虚拟光标滑到目标屏幕点
    ...                   # 执行 tap/scroll/drag
    cur.close()           # 会话结束
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def cursor_bin_candidates() -> tuple[Path, ...]:
    """Return cursor binary locations in ownership order.

    An explicit AGENT_CURSOR_BIN is authoritative. Otherwise use a durable
    per-user cache and retain /tmp/agent_cursor only as a legacy fallback for
    existing developer installations.
    """
    configured = os.environ.get("AGENT_CURSOR_BIN")
    if configured:
        return (Path(configured).expanduser(),)
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return (
        cache_root / "guiweave" / "bin" / "agent_cursor",
        Path("/tmp/agent_cursor"),
    )


def ensure_cursor_bin() -> str | None:
    """Locate an existing cursor binary without compiling on the action path.

    The overlay is optional. A missing binary disables visualization so it can never delay or
    prevent device input. Build/install it explicitly with bin/build_agent_cursor
    or set AGENT_CURSOR_BIN.
    """
    for candidate in cursor_bin_candidates():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


class AgentCursor:
    def __init__(self, bin_path: str | None = None):
        candidates = cursor_bin_candidates()
        self._bin = bin_path or str(candidates[0])
        self._p: subprocess.Popen | None = None

    def start(self) -> None:
        if self._p is not None and self._p.poll() is None:
            return
        self._p = subprocess.Popen(
            [self._bin], stdin=subprocess.PIPE, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _send(self, line: str) -> None:
        if self._p is None or self._p.poll() is not None:
            self.start()
        try:
            assert self._p is not None and self._p.stdin is not None
            self._p.stdin.write(line + "\n")
            self._p.stdin.flush()
        except (BrokenPipeError, AssertionError, ValueError):
            self._p = None  # 守护进程没了,下次 _send 自动重启

    def move(self, x: float, y: float) -> None:
        """把虚拟光标平滑滑到逻辑屏幕点 (x, y)(左上原点)。"""
        self._send(f"move {int(round(x))} {int(round(y))}")

    def set_mode(self, mode: str) -> None:
        """切换箭头形状: normal | scroll_up | scroll_down | scroll_left | scroll_right"""
        self._send(f"mode {mode}")

    def persist(self, on: bool = True) -> None:
        """常驻:on=True 关闭空闲自动隐藏,光标停在上次动作点不消失(browser 用——
        OS 浮层不进页面截图,常驻不污染感知)。on=False 恢复默认空闲隐藏。"""
        self._send(f"persist {1 if on else 0}")

    def show(self) -> None:
        self._send("show")

    def hide(self) -> None:
        self._send("hide")

    def close(self) -> None:
        if self._p is None:
            return
        try:
            self._send("quit")
            self._p.wait(timeout=1)
        except Exception:
            try:
                self._p.terminate()
            except Exception:
                pass
        self._p = None
