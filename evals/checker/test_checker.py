"""Checker eval runner: validates SingleCheck status and loading field against labeled cases."""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.schemas import Milestone, Observation
from policy_expr.supervisor.milestone import run_checker

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

passed = 0
failed = 0
fallback_count = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:45s}"
    if detail:
        line += f"  {detail}"
    print(line)


def test_checker() -> None:
    global fallback_count
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        png_bytes = (PROJECT_ROOT / c["screenshot"]).read_bytes()
        observation = Observation(png_bytes=png_bytes, source="eval")
        m = c["milestone"]
        milestone = Milestone.model_validate({**m, "id": c["label"]})

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                result = run_checker(
                    milestone, observation, [],
                    app_name=m["app_name"],
                    task_type=m.get("task_type", "action"),
                )
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue
        finally:
            output = buf.getvalue()
            if "改用纯文本 JSON 解析" in output:
                fallback_count += 1
            print(output, end="")

        expected = c["expected"]
        details = []
        if result.status != expected["status"]:
            details.append(f'status: expected {expected["status"]!r}, got {result.status!r}')
        if "loading" in expected and result.loading != expected["loading"]:
            details.append(f'loading: expected {expected["loading"]}, got {result.loading}')

        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       reason: {result.reason[:100]}")


def main():
    print("── Checker Eval ──")
    test_checker()
    fallback_note = f"  ({fallback_count} fallback{'s' if fallback_count != 1 else ''})" if fallback_count else ""
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed{fallback_note}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
