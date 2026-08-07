#!/usr/bin/env python
"""Test the ScreenTableProcessor end-to-end on a live device.

Runs the screen-level table processor (reusing the statement base modules:
perception -> collection_candidates -> screen LLM decision -> AndroidExecutor
actions -> move_collection advance), logging every screen's perception, decision,
actions and scrolls to ``logs/screen_table/<timestamp>/`` for debugging.

Usage:
    uv run python scripts/screen_table_test.py "删除所有短袖T恤衬衫"
    uv run python scripts/screen_table_test.py "删除所有短袖T恤衬衫" --dry-run
    uv run python scripts/screen_table_test.py "勾选所有T恤" --dry-run

Options:
    --dry-run   sense + decide only; do NOT execute actions or scroll
    --max-screens N   limit the number of screens scanned (default 15)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.runtime.factory import build_platform  # noqa: E402


def _log_dir() -> Path:
    base = PROJECT_ROOT / "logs" / "screen_table"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = base / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default="短袖T恤衬衫", help="目标描述")
    parser.add_argument("--action", default="删除", help="采取行动（自由文本）")
    parser.add_argument("--dry-run", action="store_true", help="sense + decide only")
    parser.add_argument("--max-screens", type=int, default=15)
    args = parser.parse_args()

    log_dir = _log_dir()
    print(f"=== ScreenTableProcessor 测试 ===")
    print(f"target: {args.target}")
    print(f"action: {args.action}")
    print(f"dry-run: {args.dry_run}")
    print(f"log dir: {log_dir}")

    bundle = build_platform("android")
    with bundle.open_session() as platform:
        from gui_agent.core.execution.screen_table import ScreenTableProcessor

        processor = ScreenTableProcessor(
            bundle, platform, log_dir, max_screens=args.max_screens,
        )
        # foreach 语义：设置目标 + 行动（手动循环需显式设置）。
        processor.target = args.target
        processor.action = args.action
        processor.processed_rows.clear()
        # Run screen by screen manually so we can log each step and honor dry-run.
        rows_by_screen: list[dict] = []
        stable_empty = 0
        seen_names: set[str] = set()
        no_progress = 0
        screen_no = 0
        while screen_no < args.max_screens:
            screen_no += 1
            obs = processor._observe()
            rows = processor._rows(obs)
            visible = [r for r in rows if r["buttons"]]
            record = {
                "screen": screen_no,
                "visible_rows": [r["name"] for r in visible],
                "all_rows": [r["name"] for r in rows],
                "buttons": {
                    r["name"]: {k: list(v) for k, v in r["buttons"].items()}
                    for r in visible
                },
            }
            # Save screenshot for this screen.
            png = getattr(obs, "png_bytes", b"")
            if png:
                (log_dir / f"screen_{screen_no:02d}.png").write_bytes(png)

            if not visible:
                stable_empty += 1
                record["decision"] = {"scroll": "none", "reason": "no visible rows"}
                rows_by_screen.append(record)
                if stable_empty >= 2:
                    print(f"屏{screen_no}: 无可视行 x2，终止")
                    break
                if not args.dry_run:
                    processor._advance(obs)
                continue
            stable_empty = 0

            decision = processor._decide_screen(obs, visible)
            record["decision"] = decision.model_dump()
            new_names = [r["name"] for r in visible if r["name"] not in seen_names]
            seen_names.update(r["name"] for r in visible)

            acted = False
            record["executions"] = []
            for matched in decision.matched_rows:
                if matched.row in processor.processed_rows:
                    record["executions"].append({
                        "row": matched.row, "skipped": "already processed",
                    })
                    continue
                if args.dry_run:
                    result = f"(dry-run) would {processor.action} {matched.row}"
                else:
                    result = processor._execute_row(obs, matched)
                processor.processed_rows.add(matched.row)
                acted = True
                record["executions"].append({
                    "row": matched.row, "action": processor.action, "result": result,
                })

            rows_by_screen.append(record)
            print(
                f"屏{screen_no}: {len(visible)}行 new={len(new_names)} "
                f"rows={[a.row for a in decision.matched_rows]} scroll={decision.scroll} "
                f"done={decision.done} 新动作={acted}"
            )

            if decision.done:
                break
            if not new_names and not acted:
                no_progress += 1
                if no_progress >= 2:
                    print(f"屏{screen_no}: 无新行且无新动作 x2，终止")
                    break
            else:
                no_progress = 0
            if not args.dry_run:
                processor._advance(obs, direction=decision.scroll if decision.scroll in {"up", "down"} else "down")
            time.sleep(0.5)

        summary = {
            "target": args.target,
            "action": args.action,
            "dry_run": args.dry_run,
            "screens": rows_by_screen,
            "processed_rows": sorted(processor.processed_rows),
        }
        out = log_dir / "result.json"
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n=== 结果写入 {out} ===")
        print(f"processed_rows: {summary['processed_rows']}")


if __name__ == "__main__":
    main()
