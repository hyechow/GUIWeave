"""Reply eval runner. Loads cases from cases.json and runs generate_reply."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from gui_agent.core.llm.output import generate_reply

CASES_FILE = Path(__file__).parent / "cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def check(reply: str, expected: dict) -> tuple[bool, str]:
    errors = []
    for phrase in expected.get("must_contain", []):
        if phrase not in reply:
            errors.append(f'missing "{phrase}"')
    for phrase in expected.get("must_not_contain", []):
        if phrase in reply:
            errors.append(f'should not contain "{phrase}"')
    any_of = expected.get("must_contain_any", [])
    if any_of and not any(p in reply for p in any_of):
        errors.append(f'must contain one of {any_of}')
    return (len(errors) == 0), "; ".join(errors)


def run():
    cases = load_cases()
    passed = failed = 0

    for c in cases:
        try:
            reply = generate_reply(
                goal=c["goal"],
                result=c.get("result"),
                session=c.get("session"),
                non_action_reason=c.get("non_action_reason", ""),
                content_notes=c.get("content_notes"),
                collection_context=c.get("collection_context"),
            )
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {c['label']:35s} → exception: {e}")
            continue

        ok, detail = check(reply, c["expected"])
        passed += ok
        failed += not ok

        tag = "PASS" if ok else "FAIL"
        # trim reply for display
        display = reply.replace("\n", " ")[:80]
        print(f"  [{tag}] {c['label']:35s} → {display}")
        if detail:
            print(f"         {detail}")

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
