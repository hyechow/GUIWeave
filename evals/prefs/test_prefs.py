"""Prefs eval runner: CRUD logic (inline) + LLM extraction (from cases.json)."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from gui_agent.core.prefs import PreferenceManager, extract_prefs_llm

CASES_FILE = Path(__file__).parent / "cases.json"


# ── Helpers ─────────────────────────────────────────────────────────────────

passed = 0
failed = 0

def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:30s}"
    if detail and not ok:
        line += f"  {detail}"
    print(line)


# ── CRUD tests (no LLM) ───────────────────────────────────────────────────


def test_crud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "prefs.json"
        pm = PreferenceManager(path=path)

        pm.set_app_pref("外卖", "美团", source="auto")
        _report("CRUD-set", pm.lookup_app("外卖") == "美团")

        pm.set_app_pref("外卖", "饿了么", source="auto")
        _report("CRUD-update-auto", pm.lookup_app("外卖") == "饿了么")

        pm.set_app_pref("外卖", "美团", source="manual")
        pm.set_app_pref("外卖", "饿了么", source="auto")
        _report("CRUD-manual-priority", pm.lookup_app("外卖") == "美团")

        pm.set_app_pref("打车", "滴滴", source="manual")
        ok_del = pm.remove_app_pref("打车")
        _report("CRUD-delete", ok_del and pm.lookup_app("打车") is None)

        pm.set_app_pref("外卖", "美团", source="manual")
        pm.set_app_pref("打车", "滴滴", source="auto")
        text = pm.format_prefs_for_prompt()
        _report("CRUD-format", "美团" in text and "滴滴" in text)

        pm2 = PreferenceManager(path=path)
        _report("CRUD-persist", pm2.lookup_app("外卖") == "美团")


# ── LLM extraction tests ──────────────────────────────────────────────────


def test_extract() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        try:
            result = extract_prefs_llm(c["user_msg"], c["goal"], c["session"])
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        ok = True
        details = []
        for key, expected_val in c["expected"].items():
            got = getattr(result, key, None)
            if isinstance(expected_val, list):
                if sorted(got) != sorted(expected_val):
                    ok = False
                    details.append(f'{key}: expected {expected_val}, got {got}')
            else:
                if expected_val and not got:
                    ok = False
                    details.append(f'{key}: expected "{expected_val}", got empty')
                elif expected_val and expected_val not in str(got):
                    ok = False
                    details.append(f'{key}: expected "{expected_val}", got "{got}"')
                elif not expected_val and got:
                    ok = False
                    details.append(f'{key}: expected empty, got "{got}"')

        _report(c["label"], ok, "; ".join(details) if details else "")


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    print("── CRUD (no LLM) ──")
    test_crud()
    print(f"\n── LLM Extraction ──")
    test_extract()

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
