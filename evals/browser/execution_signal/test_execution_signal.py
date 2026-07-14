"""Offline replay of typed execution evidence distilled from browser failures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from gui_agent.core.run.execution_signals import (
    EvidenceClaim,
    ExecutionCoordinator,
    ExecutionContract,
)


CASES = Path(__file__).with_name("cases.json")


def main() -> int:
    cases = json.loads(CASES.read_text())
    coordinator = ExecutionCoordinator()
    failures: list[str] = []
    for case in cases:
        scope = case["scope"]
        contract_data = dict(case["contract"])
        contract_data["output_fields"] = tuple(contract_data.get("output_fields", ()))
        contract = ExecutionContract(**contract_data)
        claims = []
        for data in case["claims"]:
            item = dict(data)
            authoritative = bool(item.pop("authoritative", False))
            claims.append(EvidenceClaim(
                scope=scope,
                authoritative_for=((item["domain"],) if authoritative else ()),
                **item,
            ))
        decision = coordinator.decide(contract, claims, scope=scope)
        got = {
            "next": decision.next,
            "completion_status": decision.completion_status,
        }
        if got != case["expected"]:
            failures.append(f"{case['label']}: expected={case['expected']} got={got}")
    if failures:
        print("\n".join(failures))
        return 1
    print(f"execution signal eval: {len(cases)} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
