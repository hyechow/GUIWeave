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
