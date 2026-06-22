"""Browser DOM-snap evals for execution-layer click retargeting.

These cases are deterministic and do not need a live browser. They exercise the
BrowserExecutor wiring around ``client.dom_snap`` using log-derived coordinates.

Run:
  uv run python evals/browser/dom_snap/test_dom_snap.py
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.executor import BrowserExecutor

CASES_FILE = Path(__file__).parent / "cases.json"


class _FakeClient:
    def __init__(self, *, viewport: tuple[int, int], snap: tuple[float, float, str | None]):
        self._viewport = viewport
        self._snap = snap
        self.clicked: tuple[float, float] | None = None

    @property
    def viewport_size(self) -> tuple[int, int]:
        return self._viewport

    def dom_snap(self, x: float, y: float, target_text: str = "") -> tuple[float, float, str | None]:
        return self._snap

    def tap(self, x: float, y: float) -> str:
        self.clicked = (x, y)
        return f"OK tap ({x:.0f},{y:.0f})"


def _check_case(case: dict) -> list[str]:
    viewport = tuple(case["viewport"])
    snap = case["dom_snap"]
    client = _FakeClient(viewport=viewport, snap=(snap["x"], snap["y"], snap.get("info")))
    executor = BrowserExecutor(types.SimpleNamespace(client=client))
    decision = BrowserActionDecision(action=BrowserAction(**case["action"]))

    executor.execute(decision)
    if client.clicked is None:
        return ["no click was executed"]

    x, y = client.clicked
    exp = case["expected"]
    details: list[str] = []
    if "clicked_x_min" in exp and x < exp["clicked_x_min"]:
        details.append(f"clicked x {x:.1f} < min {exp['clicked_x_min']}")
    if "clicked_x_max" in exp and x > exp["clicked_x_max"]:
        details.append(f"clicked x {x:.1f} > max {exp['clicked_x_max']}")
    if "clicked_y_min" in exp and y < exp["clicked_y_min"]:
        details.append(f"clicked y {y:.1f} < min {exp['clicked_y_min']}")
    if "clicked_y_max" in exp and y > exp["clicked_y_max"]:
        details.append(f"clicked y {y:.1f} > max {exp['clicked_y_max']}")

    snap_info = ""
    if isinstance(decision.action.snap, dict):
        snap_info = str(decision.action.snap.get("info") or "")
    for bad in exp.get("snap_info_not_contains", []):
        if bad in snap_info:
            details.append(f"snap info should not contain {bad!r}")
    return details


def main() -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    passed = 0
    failed = 0
    print("-- Browser DOM Snap Eval --")
    for case in cases:
        details = _check_case(case)
        ok = not details
        passed += int(ok)
        failed += int(not ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {case['label']}")
        if details:
            print(f"        {'; '.join(details)}")
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
