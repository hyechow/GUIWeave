"""Inspect or execute action-policy decisions.

Usage:
    uv run python scripts/action_policy_test.py "把年份调小一格"
    uv run python scripts/action_policy_test.py --image /tmp/action_policy_current.png "把月份调大一格"
    uv run python scripts/action_policy_test.py --preset picker
    uv run python scripts/action_policy_test.py --execute "把年份调小一格"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_agent.adapters.iphone.executor import ActionExecutor
from gui_agent.adapters.iphone.perception import LivePhoneSession
from gui_agent.core.policies import StructuredOutputPolicy
from gui_agent.core.schemas import Observation


PRESETS = {
    "picker": [
        "把年份调小一格",
        "把月份调大一格",
        "把日期调小一格",
    ],
    "scroll": [
        "向下滚动查看更多内容",
        "向上滚动回到上方内容",
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Test StructuredOutputPolicy decisions")
    parser.add_argument("instructions", nargs="*", help="Instructions to test")
    parser.add_argument("--image", type=Path, help="Use a saved screenshot instead of live phone")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="Run a built-in instruction set")
    parser.add_argument("--save-image", type=Path, help="Save the live screenshot used for the test")
    parser.add_argument("--execute", action="store_true", help="Execute each action against the live phone")
    parser.add_argument("--after-dir", type=Path, help="Save screenshots after executed actions")
    parser.add_argument("--dump-case", action="store_true", help="Print cases.json entry for each decision")
    args = parser.parse_args()
    if args.execute and args.image:
        raise SystemExit("--execute requires a live phone session; remove --image")

    instructions = list(args.instructions)
    if args.preset:
        instructions.extend(PRESETS[args.preset])
    if not instructions:
        instructions = ["向下滚动查看更多内容"]

    policy = StructuredOutputPolicy()

    if args.image:
        png = args.image.read_bytes()
        _run_decisions(
            policy, png, str(args.image), instructions,
            dump_case=args.dump_case,
        )
        return

    with LivePhoneSession() as phone:
        executor = ActionExecutor(phone)
        png = phone.screenshot()
        if args.save_image:
            args.save_image.parent.mkdir(parents=True, exist_ok=True)
            args.save_image.write_bytes(png)

        for idx, instruction in enumerate(instructions, start=1):
            screenshot_rel = ""
            if args.dump_case and args.save_image:
                try:
                    screenshot_rel = args.save_image.relative_to(Path.cwd()).as_posix()
                except ValueError:
                    screenshot_rel = str(args.save_image)
            decision = _decide_one(
                policy, png, "live", instruction,
                dump_case=args.dump_case,
                screenshot_rel=screenshot_rel,
            )
            if not args.execute:
                continue
            if decision.not_found_reason:
                print("skip execute: not_found_reason present")
                continue
            ok = executor.execute(decision)
            print(f"executed: {ok}")
            time.sleep(0.8)
            png = phone.screenshot()
            if args.after_dir:
                args.after_dir.mkdir(parents=True, exist_ok=True)
                (args.after_dir / f"after_{idx}.png").write_bytes(png)


def _run_decisions(
    policy: StructuredOutputPolicy,
    png: bytes,
    source: str,
    instructions: list[str],
    *,
    dump_case: bool = False,
) -> None:
    for instruction in instructions:
        _decide_one(policy, png, source, instruction, dump_case=dump_case, screenshot_rel=source)


def _decide_one(
    policy: StructuredOutputPolicy,
    png: bytes,
    source: str,
    instruction: str,
    *,
    dump_case: bool = False,
    screenshot_rel: str = "",
):
    print(f"Screenshot source: {source}")
    print(f"\n=== {instruction} ===")
    obs = Observation(png_bytes=png, source=source)
    decision = policy.decide(obs, instruction)
    print(json.dumps(decision.action.model_dump(exclude_none=True), ensure_ascii=False, indent=2))
    if decision.not_found_reason:
        print(f"not_found_reason: {decision.not_found_reason}")

    if dump_case:
        expected_fields = {
            "action_type": decision.action.action_type,
        }
        for key in ("direction", "value_direction", "target_area", "amount", "method"):
            val = getattr(decision.action, key, None)
            if val is not None:
                expected_fields[key] = val
        case = {
            "label": instruction,
            "screenshot": screenshot_rel or source,
            "instruction": instruction,
            "expected": expected_fields,
        }
        print(f"\n--- cases.json entry ---")
        print(json.dumps(case, ensure_ascii=False, indent=2))
        print(f"--- end ---\n")

    return decision


if __name__ == "__main__":
    main()
