"""Android structured_read eval: 截图 → 读指定字段（纯视觉）。

测 ``gui_agent.core.orchestrator.structured_read.structured_read`` —— orchestrator 的视觉
读取原语（reader LLM 从截图读 returns 字段）。调真实 LLM，按需跑（非确定；判改动多跑几次）。

Run: uv run python evals/android/structured_read/test_structured_read.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator.structured_read import structured_read

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}  {detail}")


def main() -> int:
    print("── Android Structured-Read Eval ──")
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        p = PROJECT_ROOT / c["screenshot"]
        if not p.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue
        png = p.read_bytes()
        try:
            # prepare_vision_prompt_png=None: 截图已是 android 降采样后的尺寸（贴近真实 reader 输入）
            reads = structured_read(png, c["returns"], read_spec=c.get("read_spec", ""))
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details: list[str] = []
        for field in c["returns"]:
            val = (reads.get(field) or "").strip()
            if c.get("expected_nonempty") and not val:
                details.append(f"{field} 读空（应非空）")
            elif val and not re.fullmatch(r"\d+", val):
                details.append(f"{field}={val!r} 不是纯数字")
        detail = f"reads={reads}" + ("；" + "；".join(details) if details else "")
        _report(c["label"], not details, detail)

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
