#!/usr/bin/env python3
"""Preview the AgentHUD floating panel (the ``--hud`` overlay) without running an agent.

The panel's tkinter rendering differs noticeably from any static mock (line heights,
padding, CJK glyph extents), so the reliable way to tune its look is to launch the
REAL widget. This shows the two-zone layout — TASK (goal) over DECISION (live status)
— with sample text, then closes after a few seconds.

Defaults mirror the browser HUD size (``core.ui.hud.dock_rect``), so re-running this
after tweaking the layout/size reflects exactly what an agent run would show.

Usage:
    uv run python scripts/preview_hud.py
    uv run python scripts/preview_hud.py --width 600 --height 160 --secs 15
    uv run python scripts/preview_hud.py --goal "..." --status "..."
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui_agent.core.ui.hud import AgentHUD, dock_rect

# A long decision line on purpose, to check the two-line case isn't clipped.
DEFAULT_GOAL = "搜索 NVIDIA 在 2026-06-09 的股价"
DEFAULT_STATUS = "Turn 2 — 当前已在飞书云文档主页，点击右上角新建按钮并选择文档创建空白文档以继续任务"


def main() -> int:
    # Browser HUD default size, straight from dock_rect (kept in sync automatically).
    _, _, def_w, def_h = dock_rect(0, 0, 1440, 900)

    ap = argparse.ArgumentParser(description="Preview the AgentHUD panel.")
    ap.add_argument("--width", type=int, default=def_w)
    ap.add_argument("--height", type=int, default=def_h)
    ap.add_argument("--x", type=int, default=500, help="screen x of the panel")
    ap.add_argument("--y", type=int, default=400, help="screen y of the panel")
    ap.add_argument("--alpha", type=float, default=0.92)
    ap.add_argument("--goal", default=DEFAULT_GOAL)
    ap.add_argument("--status", default=DEFAULT_STATUS)
    ap.add_argument("--secs", type=float, default=12.0, help="how long to keep it on screen")
    args = ap.parse_args()

    hud = AgentHUD(origin=(args.x, args.y), width=args.width, height=args.height, alpha=args.alpha)
    hud.set_goal(args.goal)
    hud.update(args.status)
    print(f"HUD 显示中 {args.secs:g}s（{args.width}×{args.height} @ {args.x},{args.y}）… Ctrl+C 提前关闭")
    try:
        time.sleep(args.secs)
    except KeyboardInterrupt:
        pass
    finally:
        hud.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
