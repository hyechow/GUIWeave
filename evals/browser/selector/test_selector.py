"""KnowledgeSelector eval: section selection quality on synonym traps.

Self-contained — each case carries its own section list (title + when one-liner) inline,
so it runs without the local knowledge/ dir. Locks the failure mode where a bare-title
manifest picks the literally-matching-but-wrong section (「如何添加机器人」= real-device
registration) over the semantically-right one (「如何使用机器人模拟器」= virtual robots)
for a 虚拟机器人 task — and the reverse control so `when` lines don't overcorrect.

Run:  uv run python evals/browser/selector/test_selector.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.schemas import Milestone
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge
from gui_agent.core.supervisor.milestone.helpers import run_selector

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:64s}"
    if detail:
        line += f"  {detail}"
    print(line)


def test_selector() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        # Inline sections → raw texts with `when:` frontmatter, same shape production loads.
        pk = ProgressiveKnowledge({
            stem: f"---\nwhen: {when}\n---\n（正文略）"
            for stem, when in c["sections"].items()
        })
        ms = Milestone.model_validate({"id": "m1", **c["milestone"]})
        try:
            sel = run_selector(c["goal"], ms, c["page_identity"], pk.selector_manifest())
            picked = pk.by_ids(sel.section_ids)
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = []
        exp = c["expected"]
        for stem in exp.get("must_include", []):
            if stem not in picked:
                details.append(f"未选中必选章节「{stem}」")
        for stem in exp.get("must_exclude", []):
            if stem in picked:
                details.append(f"选中了禁选章节「{stem}」")
        ok = len(details) == 0
        _report(c["label"], ok, ("; ".join(details) + f"  picked={picked}") if details else f"picked={picked}")


def main() -> int:
    print("── Browser KnowledgeSelector Eval ──")
    test_selector()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
