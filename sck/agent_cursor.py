"""Agent 虚拟光标客户端 —— 驱动 sck/agent_cursor.swift 守护进程。

项目自有的 agent 光标可视化:一个透明、点击穿透、不抢焦点的 overlay,蓝渐变箭头 + bloom,
跨点平滑滑动。click/scroll/drag 在动作前调用 move(),全局只有这一个 agent 光标;真实物理
光标不动。坐标 = 逻辑屏幕点、左上原点(同 CGWindow 全局坐标)。

独立 overlay 进程(不整合进 mirror_daemon,避免浮层进截图污染 OCR/YOLO)。
二进制路径默认 /tmp/agent_cursor,可用环境变量 AGENT_CURSOR_BIN 覆盖。
  build: swiftc sck/agent_cursor.swift -o /tmp/agent_cursor

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


def ensure_cursor_bin() -> str | None:
    """定位 agent_cursor 二进制(env AGENT_CURSOR_BIN 或 /tmp/agent_cursor);缺失则从同目录
    的 agent_cursor.swift 编译一次。返回路径,或 None(调用方据此禁用光标可视化)。

    与 iphone mirror_daemon 的 _ensure_cursor_bin 同款逻辑,放在 sck/ 这个 agent_cursor
    的“家”里,供任意平台(iphone/browser)复用同一个 overlay 渲染器。"""
    cb = os.environ.get("AGENT_CURSOR_BIN") or "/tmp/agent_cursor"
    src = Path(__file__).resolve().parent / "agent_cursor.swift"
    # Up-to-date binary (exists AND not older than the source) → reuse. Rebuilding
    # when the .swift is newer means edits (e.g. the persist command) aren't masked
    # by a stale cached build.
    if os.path.exists(cb) and (not src.exists() or os.path.getmtime(cb) >= os.path.getmtime(src)):
        return cb
    try:
        subprocess.run(["swiftc", str(src), "-o", cb], check=True, capture_output=True)
        return cb
    except Exception:
        return cb if os.path.exists(cb) else None  # fall back to a stale build if present


class AgentCursor:
    def __init__(self, bin_path: str | None = None):
        self._bin = bin_path or os.environ.get("AGENT_CURSOR_BIN") or "/tmp/agent_cursor"
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
