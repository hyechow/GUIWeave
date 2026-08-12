"""Command-line interface for the Tool Agent-only GUIWeave distribution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from gui_agent.core.tool_agent.service import ToolAgentService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guiweave")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="check one platform environment")
    check.add_argument("platform", choices=("browser", "android"))
    check.add_argument("--cdp-url")
    check.add_argument("--adb-serial")
    check.add_argument("--headless", action="store_true")

    run = subparsers.add_parser("run", help="run a Tool Agent task")
    run.add_argument("platform", choices=("browser", "android"))
    run.add_argument("goal")
    run.add_argument("--perception", choices=("vision-only", "enhanced"), default="enhanced")
    run.add_argument("--max-turns", type=int, default=50)
    run.add_argument("--cdp-url")
    run.add_argument("--adb-serial")
    run.add_argument("--headless", action="store_true")
    run.add_argument("--no-hud", action="store_true")
    run.add_argument(
        "--multi-action",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    run.add_argument("--log-root", type=Path)
    return parser


def _platform_options(args: argparse.Namespace) -> dict[str, object]:
    if args.platform == "browser":
        return {"cdp_url": args.cdp_url, "headless": args.headless}
    return {"serial": args.adb_serial}


def main() -> int:
    load_dotenv()
    args = _build_parser().parse_args()
    service = ToolAgentService(log_root=getattr(args, "log_root", None))
    options = _platform_options(args)
    if args.command == "check":
        result = service.check_environment(args.platform, **options)
        print(json.dumps({
            "ok": result.ok,
            "summary": result.summary,
            "lines": list(result.lines),
        }, ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    result = service.run(
        args.goal,
        platform=args.platform,
        perception_mode=args.perception,
        max_turns=args.max_turns,
        allow_multi_action=args.multi_action,
        show_hud=not args.no_hud and not args.headless,
        mirror_stdio=True,
        **options,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.phase == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
