"""Decomposer eval: validates milestone structure from goal + screenshot."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.schemas import Observation
from policy_expr.supervisor.milestone import MilestoneSupervisorPolicy

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:40s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_milestones(milestones: list, expected: dict) -> list[str]:
    details = []
    all_text = " ".join(f"{m.name} {m.description}" for m in milestones)

    if "min_milestones" in expected and len(milestones) < expected["min_milestones"]:
        details.append(f"expected >={expected['min_milestones']} milestones, got {len(milestones)}")

    for kw in expected.get("any_milestone_contains", []):
        if kw not in all_text:
            details.append(f"no milestone mentions '{kw}'")

    for kw in expected.get("no_milestone_contains", []):
        if kw in all_text:
            details.append(f"unexpected '{kw}' found in milestones")

    for app in expected.get("all_apps_modeled", []):
        if app not in all_text:
            details.append(f"app '{app}' not modeled in any milestone")

    return details


def test_decomposer() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue

        png_bytes = screenshot_path.read_bytes()
        observation = Observation(png_bytes=png_bytes, source="eval")

        try:
            policy = MilestoneSupervisorPolicy()
            policy._decompose(c["goal"], observation)
            milestones = [policy._milestones[mid] for mid in policy._order]
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_milestones(milestones, c["expected"])
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            for m in milestones:
                print(f"       [{m.kind}] {m.name}: {m.success_condition}")


def main():
    print("── Decomposer Eval ──")
    test_decomposer()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
