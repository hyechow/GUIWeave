"""Browser intent-resolver eval: validates entity classification (precise vs fuzzy + search key).

The Intent Resolver runs once on the user goal (text-only, no screenshot) and tags each lookup entity
with {type, match_mode, search_key}. This drives the decomposer's column choice and the
exact-then-fuzzy retrieval ladder. Cases assert, per expected entity, the type / match_mode and an
accepted set of search keys (the exact key token can vary, so it's an any-of).

Seeded from webarena task-113 (20260622_124258): 'Olivia zip jacket' is a PRODUCT referred to
approximately (real name 'Olivia 1/4 Zip Light Jacket'); it must be product/approximate with a single
distinctive key ('Olivia'), so the plan filters the Product column by keyword rather than the review
text by the full phrase.

Run:  uv run python evals/browser/intent_resolver/test_intent_resolver.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator.intent_resolver import resolve_intent

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _find(entities, mention_sub: str):
    for e in entities:
        if mention_sub in e.mention:
            return e
    return None


def test_intent_resolver() -> None:
    global passed, failed
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        try:
            r = resolve_intent(c["goal"])
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {c['label']:60s}  exception: {e}")
            continue

        details: list[str] = []
        for exp in c["expected_entities"]:
            e = _find(r.entities, exp["mention_contains"])
            if e is None:
                details.append(f"no entity matching {exp['mention_contains']!r} (got {[x.mention for x in r.entities]})")
                continue
            if "type" in exp and e.type != exp["type"]:
                details.append(f"{exp['mention_contains']}: type {e.type!r} != {exp['type']!r}")
            if "match_mode" in exp and e.match_mode != exp["match_mode"]:
                details.append(f"{exp['mention_contains']}: mode {e.match_mode!r} != {exp['match_mode']!r}")
            key_any = exp.get("search_key_any")
            if key_any and e.search_key not in key_any:
                details.append(f"{exp['mention_contains']}: key {e.search_key!r} not in {key_any}")

        ok = not details
        passed += ok
        failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {c['label']}")
        if not ok:
            print(f"        {'; '.join(details)}")


def main() -> int:
    print("── Browser Intent-Resolver Eval ──")
    test_intent_resolver()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
