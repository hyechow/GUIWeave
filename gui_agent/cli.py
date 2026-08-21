"""Command-line interface for the Tool Agent-only GUIWeave distribution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from gui_agent.adapters.browser.factory import DEFAULT_BROWSER_START_URL
from gui_agent.core.tool_agent.service import ToolAgentService
from gui_agent.core.runtime.platforms import PLATFORMS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guiweave")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="check one platform environment")
    check.add_argument("platform", choices=PLATFORMS)
    check.add_argument("--cdp-url")
    check.add_argument("--adb-serial")
    check.add_argument("--headless", action="store_true")
    check.add_argument(
        "--browser-profile", choices=("evaluation", "production"),
        help="production requires headed Chrome and uses low-intrusion browser sensing",
    )

    run = subparsers.add_parser("run", help="run a Tool Agent task")
    run.add_argument("platform", choices=PLATFORMS)
    run.add_argument("goal")
    run.add_argument("--perception", choices=("vision-only", "enhanced"), default="enhanced")
    run.add_argument("--max-turns", type=int, default=50)
    run.add_argument("--cdp-url")
    run.add_argument(
        "--start-url",
        default=DEFAULT_BROWSER_START_URL,
        help=f"browser initial URL (default: {DEFAULT_BROWSER_START_URL})",
    )
    run.add_argument("--adb-serial")
    run.add_argument("--headless", action="store_true")
    run.add_argument(
        "--browser-profile", choices=("evaluation", "production"),
        help="production requires headed Chrome and uses low-intrusion browser sensing",
    )
    run.add_argument("--no-hud", action="store_true")
    run.add_argument(
        "--multi-action",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    run.add_argument("--log-root", type=Path)

    console = subparsers.add_parser("console", help="start the local Run Console")
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=7468)
    return parser


def _platform_options(args: argparse.Namespace) -> dict[str, object]:
    if args.platform == "browser":
        options: dict[str, object] = {
            "cdp_url": args.cdp_url,
            "headless": args.headless,
        }
        if profile := getattr(args, "browser_profile", None):
            options["browser_profile"] = profile
        if hasattr(args, "start_url"):
            options["start_url"] = args.start_url
        return options
    if args.platform == "android":
        return {"serial": args.adb_serial}
    return {}


def main() -> int:
    load_dotenv()
    args = _build_parser().parse_args()
    if args.command == "console":
        import uvicorn

        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Run Console is local-only; host must be a loopback address")
        uvicorn.run(
            "gui_agent.console:app",
            host=args.host,
            port=args.port,
            log_level="warning",
        )
        return 0
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
