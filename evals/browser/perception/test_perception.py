"""Browser perception eval: form-control classification (deterministic, no LLM, no live site).

Tests that ``gui_agent.adapters.browser.form_reader.form_controls_js`` classifies DOM controls
correctly — especially Magento custom controls that LOOK like ``<input>`` but are actually
select-style dropdowns.

Seeded from task 63 (logs/gui_agent/webarena/browser/20260629_104539): the per-page control is a
knockout ``selectmenu`` whose visible ``<input type=text>`` is just the display box. It was
classified as ``text_input``, so the planner used ``type`` to fill "100" — but ``type`` sets the
display value without triggering knockout's ``setSize``, so the grid stayed at 20 rows → partial →
data_query failed (and the typed value even landed in the wrong box). The selectmenu must be
classified as a select-like control carrying its options, so the planner routes to ``select_option``
(mouse-click the option button → setSize fires → grid refreshes).

Deterministic: loads an HTML fixture in headless chromium, runs ``form_controls_js``, asserts the
normalized kind/options. No live browser, no LLM.

Run:  uv run python evals/browser/perception/test_perception.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.adapters.browser.form_reader import form_controls_js, normalize_form_controls

FIXTURES = Path(__file__).parent / "fixtures"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:60s}  {detail}".rstrip())


def _run_fixture(html_file: str) -> list[dict]:
    """Load an HTML fixture in headless chromium, run form_controls_js, return normalized controls."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content((FIXTURES / html_file).read_text(encoding="utf-8"))
        raw = page.evaluate(form_controls_js())
        browser.close()
    if isinstance(raw, str):
        raw = json.loads(raw)
    return normalize_form_controls(raw)


def test_selectmenu_classified_as_select_with_options() -> None:
    controls = _run_fixture("selectmenu.html")
    per_page = next(
        (c for c in controls
         if c.get("kind") == "selectmenu" or "per page" in c.get("label", "").lower()),
        None,
    )
    if per_page is None:
        _report("selectmenu: 找到 per page 控件", False, f"controls={[c.get('kind') for c in controls]}")
        return
    _report("selectmenu: 找到 per page 控件", True)
    # 核心:不得是 text_input(否则 planner 用 type 填值,不触发 setSize)
    _report(
        "selectmenu 分类为 selectmenu(非 text_input)",
        per_page.get("kind") == "selectmenu",
        f"kind={per_page.get('kind')!r}",
    )
    _report(
        "selectmenu 抓到 options(含 100)",
        "100" in (per_page.get("options") or []),
        f"options={per_page.get('options')}",
    )
    _report(
        "selectmenu 当前值=20",
        per_page.get("value") == "20",
        f"value={per_page.get('value')!r}",
    )


def main() -> int:
    print("── Browser Perception Eval (form-control classification) ──")
    test_selectmenu_classified_as_select_with_options()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
