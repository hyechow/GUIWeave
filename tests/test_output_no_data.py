"""Unit: an analysis task with empty content_notes must return a fixed no-data reply
WITHOUT calling the LLM — never fabricate a number.

Regression — session 20260607_100322: the collection milestone force-completed with
zero scrolls, so content_notes ended empty; generate_reply then fell through to the
ACTION prompt and the LLM hallucinated "共花费 456.80 元" (smaller than the visible
sum alone). The fix routes analysis + empty-notes to a deterministic honest reply.

No network: ChatOpenAI is replaced by a bomb that fails if .invoke() is ever reached.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy_expr import output as output_mod
from policy_expr.output import _NO_DATA_REPLY, generate_reply

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:46s}"
    if detail:
        line += f"  {detail}"
    print(line)


class _Bomb:
    """Stand-in LLM: any invoke means we wrongly fell through to an LLM prompt."""

    def invoke(self, *a, **k):
        raise AssertionError("LLM 被调用了——分析类空采集应返回固定兜底、不调 LLM")


def main() -> int:
    print("── Output No-Data Unit ──")
    # generate_reply builds ChatOpenAI(...) internally; swap it so any .invoke() blows up.
    output_mod.ChatOpenAI = lambda *a, **k: _Bomb()  # type: ignore[assignment]

    # Case 1: analysis + empty content_notes → fixed reply, no LLM, no fabricated digits.
    reply = generate_reply(
        "我上个月21号到28号用微信支付花多少钱了",
        {"task_type": "analysis", "content_notes": None},
    )
    has_digit = bool(re.search(r"\d", reply))
    ok = reply == _NO_DATA_REPLY and not has_digit
    _report("分析类+空采集 → 固定兜底、不调LLM、无数字", ok, f"has_digit={has_digit}")

    # Case 2: analysis + has content_notes → must still go through the analysis prompt
    # (i.e. reach the LLM). _Bomb raising proves routing is unchanged.
    try:
        generate_reply("goal", {"task_type": "analysis"}, content_notes=["[片段1] 账单 -4.00"])
        _report("分析类+有数据 → 走 analysis(应调LLM)", False, "未调用LLM")
    except AssertionError:
        _report("分析类+有数据 → 走 analysis(应调LLM)", True)

    # Case 3: action task + empty content_notes → unchanged, still action prompt (LLM).
    try:
        generate_reply("打开微信", {"task_type": "action", "content_notes": None})
        _report("动作类+空采集 → 走 action(应调LLM)", False, "未调用LLM")
    except AssertionError:
        _report("动作类+空采集 → 走 action(应调LLM)", True)

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def test_output_no_data() -> None:
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
