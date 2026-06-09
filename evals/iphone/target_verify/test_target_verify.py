"""Target-verify eval: does the post-action targeting check judge on/off correctly.

Renders the snapped tap point on the labeled screenshot and runs the real
verify_target. Screenshots are gitignored; cases SKIP when absent.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from gui_agent.core.target_verify import verify_target

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:36s}{('  ' + detail) if detail else ''}")


def main() -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    print("── Target-Verify Eval ──")
    for c in cases:
        path = PROJECT_ROOT / c["screenshot"]
        if not path.exists():
            print(f"  [SKIP] {c['label']:36s}  screenshot not found")
            skipped += 1
            continue
        sx, sy = c["snapped"]
        try:
            v = verify_target(path.read_bytes(), sx, sy, c["instruction"])
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue
        ok = v.on_target == c["expected_on_target"]
        _report(c["label"], ok, f"on_target={v.on_target} 落在「{v.actual_element}」")
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
