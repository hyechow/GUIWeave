"""Browser action policy eval: validates BrowserActionPolicy decisions against labeled cases.

Mirrors evals/iphone/action_policy/test_action_policy.py but drives the browser
``BrowserActionPolicy`` (vision-only web prompt) instead of the iphone one, and adds
a ``url_contains`` meta-check for the navigate action (which carries a ``url``).

First browser eval — seeded from the 2026-06-09 feishu run where "进入飞书官网" was
wrongly executed as a ``type`` into the on-page Google search box instead of a
``navigate``. Run:  uv run python evals/browser/action_policy/test_action_policy.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Observation
from gui_agent.adapters.browser.policies import BrowserActionPolicy

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:60s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_action(action, expected: dict) -> list[str]:
    details = []
    for key, expected_val in expected.items():
        actual = getattr(action, key, None)
        if isinstance(expected_val, list):
            if actual not in expected_val:
                details.append(f"{key}: expected one of {expected_val}, got {actual!r}")
        elif actual != expected_val:
            details.append(f"{key}: expected {expected_val!r}, got {actual!r}")
    return details


def test_action_policy() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if not cases:
        print("  (no cases)")
        return

    policy = BrowserActionPolicy()
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue

        obs = Observation(png_bytes=screenshot_path.read_bytes(), source="eval")
        try:
            decision = policy.decide(obs, c["instruction"], verbose=False)
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        expected = c["expected"]
        # url_contains is a meta-check (substring), not a direct field equality.
        _meta_keys = {"url_contains"}
        action_expected = {k: v for k, v in expected.items() if k not in _meta_keys}
        details = _check_action(decision.action, action_expected)

        if "url_contains" in expected:
            url = decision.action.url or ""
            if expected["url_contains"] not in url:
                details.append(f"url must contain {expected['url_contains']!r}, got {url!r}")

        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            actual = decision.action.model_dump(exclude_none=True)
            print(f"       actual: {json.dumps(actual, ensure_ascii=False)}")


def main() -> int:
    print("── Browser Action Policy Eval ──")
    test_action_policy()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
