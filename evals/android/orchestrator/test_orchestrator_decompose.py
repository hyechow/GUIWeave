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


# 策略(2026/06/26 放开 API 后):走 api 合法,但 api URL 必须 {var} 接力运行时读到的真实
# owner/repo,不许 decomposer 凭记忆硬写具体实体(幻觉)。URL 模板是通用知识(非幻觉),
# 实体来自运行时——read 地址栏 URL → {var} 构造 api URL → read JSON 字段(result-then-reference)。
# 只匹配硬写的具体实体(api.github.com/repos/owner/repo),不匹配 {var} 模板({u[owner]} 含
# 括号不匹配该字符类),也不匹配 read 字段名(stargazers_count 等合法 returns)。
_API_ENTITY_RE = re.compile(
    r"api\.github\.com/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.IGNORECASE
)
_VAR_REF_RE = re.compile(r"\{[A-Za-z_]\w*(?:\[|\})")


def _check_assertions(runs: list, assertions: list[str]) -> list[str]:
    details: list[str] = []
    for a in assertions:
        if a == "no_hardcoded_api_entity":
            # api URL 含具体 owner/repo(非 {var} 模板)= 凭记忆幻觉
            offenders = []
            for r in runs:
                for field in (r.name, getattr(r, "read_spec", "") or ""):
                    for m in _API_ENTITY_RE.finditer(field or ""):
                        window = field[max(0, m.start() - 5):m.end() + 5]
                        if not _VAR_REF_RE.search(window):
                            offenders.append(f"{r.name!r} 硬写 {m.group(0)}")
            if offenders:
                details.append(
                    "API 实体幻觉(应先 read 地址栏 URL 提取 owner/repo,再 {var} 接力构造 api URL): "
                    + "; ".join(offenders)
                )
        elif a == "has_read_step":
            # 防退化:必须有带 returns 的读数 step,不能纯 navigation 翻仓库列表(20260626 失败模式)
            if not any(r.returns for r in runs):
                details.append("plan 无任何带 returns 的 run(纯 navigation 退化,读不到目标字段)")
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
