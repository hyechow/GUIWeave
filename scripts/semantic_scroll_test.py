"""Smoke test semantic scroll execution against a live iPhone Mirroring session.

Usage:
    uv run python scripts/semantic_scroll_test.py --method auto
    uv run python scripts/semantic_scroll_test.py --method wheel
    uv run python scripts/semantic_scroll_test.py --method drag
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_agent.adapters.iphone.executor import ActionExecutor
from gui_agent.adapters.iphone.perception import LivePhoneSession
from gui_agent.core.schemas import Action, ActionDecision
from gui_agent.adapters.iphone.scroll_probe import ScrollProbe, _changed_ratio, estimate_vertical_shift


ROOT = Path(__file__).parent.parent
OUT = ROOT / "logs" / "gui_agent" / "semantic_scroll_test"


def main() -> None:
    parser = argparse.ArgumentParser(description="Test semantic scroll/drag execution")
    parser.add_argument("--method", choices=["auto", "wheel", "drag"], default="auto")
    parser.add_argument("--direction", choices=["down", "up"], default="down")
    parser.add_argument("--value-direction", choices=["increase", "decrease"])
    parser.add_argument("--amount", choices=["small", "medium", "large"], default="medium")
    parser.add_argument("--target-area", default="main_content")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    action = Action(
        action_type="scroll",
        direction=args.direction,
        value_direction=args.value_direction,
        target_area=args.target_area,
        amount=args.amount,
        method=args.method,
        description=f"语义滚动测试: {args.method}/{args.direction}/{args.amount}/{args.target_area}",
    )

    with LivePhoneSession() as phone:
        executor = ActionExecutor(phone)
        before = phone.screenshot()
        (OUT / "before.png").write_bytes(before)

        if args.method == "auto":
            result = ScrollProbe(phone, executor, OUT).probe(before, action, turn_no=1)
            after = result.after_png or phone.screenshot()
            print(
                "probe:",
                "success=",
                result.success,
                "profile=",
                result.profile,
                "reason=",
                result.reason,
            )
        else:
            executor.execute(ActionDecision(action=action))
            time.sleep(0.8)
            after = phone.screenshot()

        (OUT / "after.png").write_bytes(after)
        shift, confidence = estimate_vertical_shift(before, after)
        changed = _changed_ratio(before, after)
        print(f"shift={shift:+d}px confidence={confidence:.3f} changed={changed:.3f}")
        print(f"saved: {OUT}")


if __name__ == "__main__":
    main()
