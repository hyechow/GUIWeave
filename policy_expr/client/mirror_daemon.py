"""MirrorDaemonClient — 基于 bin/mirror_daemon 的输入 + 截图后端,整合 agent 光标可视化。

接口对齐 policy_expr/client/sync_mcp.py 的 SyncMCPClient(tap / type_text / swipe /
drag / screenshot / press_home / list_targets / status),可作输入后端 drop-in。

一个常驻 mirror_daemon 进程同时提供:
  - screenshot:SCStream 抓 iPhone 镜像窗口
  - 零抢占输入:tap / scroll / type / key / menu(home|appswitch|spotlight)
均经 stdio 协议:握手 "ready\\n" → 请求 "<cmd> args\\n" → 响应 4 字节大端长度 + payload
(screenshot→PNG;输入→OK/ERR 状态串)。

光标可视化:独立 agent_cursor overlay 进程(sck/agent_cursor.{swift,py})。tap/scroll 前
把虚拟光标滑到落点(屏幕坐标)+ 显示;screenshot 前隐藏(避免绿箭头进喂模型/OCR/YOLO 的图)。
overlay 是独立进程、不进 daemon 的 SCStream 截图区(设计 B)。

坐标:tap/scroll 的 x/y = 窗口本地逻辑像素(318×701,top-left,同 executor)。光标用屏幕点
= 镜像窗口 origin + (x,y),窗口 origin 经 Quartz 查得。

连续手势(drag/swipe)走 daemon 的 drag 命令:后台 pid 路连续 mouseDragged,真光标 0px、
前台不翻;duration_ms 换算成 steps/stepMs。app 切换器卡片上滑关 app 实测可用。
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]   # policy_expr/client/x.py → 项目根
_DAEMON = _ROOT / "bin" / "mirror_daemon"
_CURSOR_SRC = _ROOT / "sck" / "agent_cursor.swift"

# agent_cursor 驱动留在 sck/(overlay 是 device I/O 基建,不进策略核心);从那里导入
sys.path.insert(0, str(_ROOT / "sck"))
from agent_cursor import AgentCursor  # noqa: E402


def _resolve_daemon() -> str:
    return os.environ.get("MIRROR_DAEMON_BIN") or str(_DAEMON)


def _ensure_cursor_bin() -> str | None:
    """定位 agent_cursor 二进制;缺失则尝试从源码编译一次。失败返回 None(禁用光标)。"""
    cb = os.environ.get("AGENT_CURSOR_BIN", "/tmp/agent_cursor")
    if os.path.exists(cb):
        return cb
    try:
        subprocess.run(["swiftc", str(_CURSOR_SRC), "-o", cb],
                       check=True, capture_output=True)
        return cb
    except Exception:
        return None


class MirrorDaemonClient:
    zero_preempt: bool = True   # executor 用 getattr 检测：无需 activate/hover/osascript
    def __init__(self, daemon_bin: str | None = None, cursor: bool = True):
        self._bin = str(daemon_bin or _resolve_daemon())
        self._p: subprocess.Popen | None = None
        self._cursor_enabled = cursor
        self._cursor: AgentCursor | None = None
        self._cursor_shown = False
        self._win: tuple[float, float, float, float] | None = None  # (x,y,w,h)

    # --- lifecycle -----------------------------------------------------

    def connect(self):
        if not os.path.exists(self._bin):
            raise FileNotFoundError(
                f"mirror_daemon not found at {self._bin}. "
                "Build: swiftc -O sck/mirror_daemon.swift -o bin/mirror_daemon"
            )
        self._p = subprocess.Popen(
            [self._bin], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        )
        assert self._p.stdout
        line = self._p.stdout.readline()
        if line.strip() != b"ready":
            raise RuntimeError(f"mirror_daemon failed to start: {line!r}")
        self._win = self._query_window()
        if self._cursor_enabled:
            cb = _ensure_cursor_bin()
            if cb:
                self._cursor = AgentCursor(cb)
                self._cursor.start()
            else:
                print("[MirrorDaemonClient] agent_cursor 二进制缺失且编译失败,光标可视化禁用",
                      file=sys.stderr)
        print(f"[MirrorDaemonClient] 就绪 window={self._win} cursor={'on' if self._cursor else 'off'}",
              file=sys.stderr)

    def close(self):
        if self._cursor:
            self._cursor.close()
            self._cursor = None
        if self._p:
            self._p.terminate()
            self._p = None

    # --- framed stdio 协议 ---------------------------------------------

    def _readn(self, n: int) -> bytes:
        assert self._p and self._p.stdout
        buf = b""
        while len(buf) < n:
            chunk = self._p.stdout.read(n - len(buf))
            if not chunk:
                raise RuntimeError("mirror_daemon closed unexpectedly")
            buf += chunk
        return buf

    def _cmd(self, line: str) -> bytes:
        assert self._p and self._p.stdin
        self._p.stdin.write((line + "\n").encode())
        self._p.stdin.flush()
        (length,) = struct.unpack(">I", self._readn(4))
        return self._readn(length) if length else b""

    def _text(self, line: str) -> str:
        return self._cmd(line).decode("utf-8", errors="replace")

    # --- 镜像窗口 origin(给光标换算屏幕坐标)----------------------------

    def _query_window(self) -> tuple[float, float, float, float] | None:
        try:
            import Quartz
            infos = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
        except Exception:
            return None
        fallback = None
        for w in infos or []:
            if "iphone" not in str(w.get("kCGWindowOwnerName", "")).lower():
                continue
            b = w.get("kCGWindowBounds", {})
            wd, ht = b.get("Width", 0), b.get("Height", 0)
            if not (100 < wd < 500 and ht > 500 and ht > wd):
                continue
            cand = (b["X"], b["Y"], wd, ht)
            if w.get("kCGWindowIsOnscreen", False):
                return cand
            if fallback is None:
                fallback = cand
        return fallback

    # --- 光标可视化 ----------------------------------------------------

    def _cursor_to(self, lx: float, ly: float):
        """把虚拟光标滑到窗口本地点 (lx,ly) 对应的屏幕点并显示。tap/drag 调用时自动切回指针形状。"""
        if not self._cursor:
            return
        wb = self._win or self._query_window()
        if not wb:
            return
        self._cursor.set_mode("normal")
        self._cursor.move(wb[0] + lx, wb[1] + ly)
        self._cursor.show()
        self._cursor_shown = True

    def hide_cursor(self):
        if self._cursor:
            self._cursor.hide()
            self._cursor_shown = False

    def show_cursor(self):
        if self._cursor:
            self._cursor.show()
            self._cursor_shown = True

    # --- public surface(对齐 SyncMCPClient)----------------------------

    def screenshot(self) -> bytes:
        # 光标 overlay 由 daemon 的 SCContentFilter 排除在捕获之外(按可执行名定位、按 pid
        # 追踪、随重启自愈),不会进截图,故无需截图前隐藏——光标可常驻可见、零延迟。
        for _ in range(10):
            raw = self._cmd("screenshot")
            if raw:
                return self._apply_mask(raw)
            time.sleep(0.2)
        raise RuntimeError("mirror_daemon: 无可用帧")

    def _apply_mask(self, png: bytes) -> bytes:
        # 复用 perception 的设备边框遮罩(懒导入,避免循环依赖);不可用则原样返回。
        try:
            from policy_expr.perception import _apply_mcp_frame
            return _apply_mcp_frame(png)
        except Exception:
            return png

    def tap(self, x: float, y: float) -> str:
        self._cursor_to(x, y)
        return self._text(f"tap {round(x)} {round(y)}")

    def scroll(self, direction: str, amount: int = 5,
               x: float = 159, y: float = 350) -> str:
        if self._cursor:
            wb = self._win or self._query_window()
            if wb:
                self._cursor.set_mode(f"scroll_{direction}")
                self._cursor.move(wb[0] + x, wb[1] + y)
                self._cursor.show()
                self._cursor_shown = True
        return self._text(f"scroll {direction} {int(amount)} {round(x)} {round(y)}")

    def type_text(self, text: str) -> str:
        t = text.replace("\n", " ").replace("\r", " ")   # 协议按行,文本去换行
        return self._text(f"type {t}")

    def key(self, name: str, *mods: str) -> str:
        return self._text((f"key {name} " + " ".join(mods)).strip())

    def press_home(self) -> str:
        return self._text("menu home")

    def app_switcher(self) -> str:
        return self._text("menu appswitch")

    def spotlight(self) -> str:
        return self._text("menu spotlight")

    # 零抢占连续手势:daemon 的 drag 命令(后台 pid 路 mouseDragged,真光标 0px、前台不翻)。
    # duration_ms → (steps, stepMs):每步约一帧(16ms)求平滑,步数夹在 [3,60] 防事件爆炸;
    # 总拖拽时长 ≈ steps×stepMs ≈ duration_ms(另有 ~166ms 固定 focus/primer 开销)。
    def _drag_cmd(self, from_x: float, from_y: float, to_x: float, to_y: float,
                  duration_ms: int) -> str:
        import threading
        steps = max(3, min(60, round(duration_ms / 16)))
        step_ms = max(1, round(duration_ms / steps))

        # 起点先显示光标
        self._cursor_to(from_x, from_y)

        # 后台线程实时推光标位置,与 daemon drag 并行
        def _animate():
            wb = self._win or self._query_window()
            if not (self._cursor and wb):
                return
            for i in range(1, steps + 1):
                f = i / steps
                lx = from_x + (to_x - from_x) * f
                ly = from_y + (to_y - from_y) * f
                self._cursor.move(wb[0] + lx, wb[1] + ly)
                time.sleep(step_ms / 1000)

        t = threading.Thread(target=_animate, daemon=True)
        t.start()
        resp = self._text(
            f"drag {round(from_x)} {round(from_y)} {round(to_x)} {round(to_y)} {steps} {step_ms}")
        t.join()
        return resp

    def swipe(self, from_x: float, from_y: float, to_x: float, to_y: float,
              duration_ms: int = 400, cursor_mode: str | None = None) -> str:
        return self._drag_cmd(from_x, from_y, to_x, to_y, duration_ms)

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float,
             duration_ms: int = 1000, cursor_mode: str | None = None) -> str:
        return self._drag_cmd(from_x, from_y, to_x, to_y, duration_ms)

    def list_targets(self) -> str:
        return "list_targets 不支持 (mirror_daemon 只驱动 iPhone 镜像)"

    def status(self) -> str:
        alive = self._p is not None and self._p.poll() is None
        return f"mirror_daemon {'alive' if alive else 'down'}; window={self._win}; cursor={'on' if self._cursor else 'off'}"


if __name__ == "__main__":
    # 冒烟:连接 → 截图 → 后台 tap(46,195) 开时钟(光标会滑过去)→ 回主屏。
    c = MirrorDaemonClient()
    c.connect()
    try:
        png = c.screenshot()
        print(f"screenshot {len(png)} bytes")
        print("tap:", c.tap(46, 195))
        time.sleep(1.3)
        print("home:", c.press_home())
    finally:
        c.close()
