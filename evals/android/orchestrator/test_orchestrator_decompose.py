"""Android orchestrator-decompose eval: goal (+截图) -> DSL Program (run/if/finish).

测 ``gui_agent.core.orchestrator.decomposer.decompose`` —— mobileworld 默认走的 DSL
program decomposer（``--orchestrator``），不同于 ``evals/android/decomposer/`` 的 legacy
milestone DAG（那个驱动 ``MilestoneSupervisorPolicy._decompose`` 产 milestone 列表）。

调真实 LLM，按需跑（非确定性；判 prompt 改动时多跑几次）。

Run:
  uv run python evals/android/orchestrator/test_orchestrator_decompose.py
  uv run python evals/android/orchestrator/test_orchestrator_decompose.py --show-program
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import ForEach, If, Run, decompose

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}  {detail}")


def _runs(stmts: list) -> list:
    """递归收集所有 Run（含 if/foreach 嵌套）。"""
    out = []
    for s in stmts:
        if isinstance(s, Run):
            out.append(s)
        elif isinstance(s, If):
            out += _runs(s.then) + _runs(s.otherwise)
        elif isinstance(s, ForEach):
            out += _runs(s.body)
    return out


# 只匹配 URL 形式的 API/JSON 直链端点（api.*/repos//contributors?），不匹配 read 字段名
# （stars_count/contributors_count 是合法的 returns 字段名，邮件正文里 {var[stars_count]} 是
# 正确的模板接力，不是走 API）。
_API_RE = re.compile(
    r"https?://api\.|api\.github\.com|/repos/|/contributors\?",
    re.IGNORECASE,
)


def _check_assertions(runs: list, assertions: list[str]) -> list[str]:
    details: list[str] = []
    for a in assertions:
        if a == "no_api_json_direct_link":
            offenders = [r.name for r in runs if _API_RE.search(r.name or "")]
            if offenders:
                details.append(
                    f"run 走了 API/JSON 直链取数（应走网页/应用界面视觉）: {offenders}"
                )
        else:
            details.append(f"unknown assertion: {a}")
    return details


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-program", action="store_true", help="打印每个 run（debug/失败时）")
    args = ap.parse_args()

    print("── Android Orchestrator-Decompose Eval ──")
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        png_bytes = None
        shot = c.get("screenshot")
        if shot:
            p = PROJECT_ROOT / shot
            if p.exists():
                png_bytes = p.read_bytes()
        try:
            program = decompose(c["goal"], png_bytes=png_bytes)
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"decompose exception: {e}")
            continue

        runs = _runs(program.statements)
        details = _check_assertions(runs, c.get("assertions", []))
        _report(c["label"], not details, "; ".join(details) if details else "")
        if args.show_program or details:
            for r in runs:
                print(f"       [run var={r.var} returns={r.returns}] {r.name}")

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
