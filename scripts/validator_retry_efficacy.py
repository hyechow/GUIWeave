"""Validator retry-efficacy harness — measures, per ValidationIssue code, whether feeding
that issue back to the planner actually clears it on the next draft.

Why this exists
---------------
validate_program emits ~37 coded issues, each rendered into feedback_block and given ONE
retry pass. But a rule that fires and whose message does NOT reliably steer the LLM to a fix
just burns the retry budget and then ships the flawed plan anyway (production fallback). The
earlier hand-counted signal was 3W/3L/3-killed — i.e. ~2/3 of validator retries do not recover.
We could not improve it because we could not see WHICH rule's message was空转. The code on each
issue makes it measurable: tag every draft attempt with the codes it fired, then for each code
ask "was it in the feedback at attempt N and gone at attempt N+1?" — that's its clear-rate.

What to read in the output
--------------------------
- clear_rate LOW  → the message_for_llm isn't steering a fix; reword it, or (if the LLM
  structurally can't fix it) convert the rule to a deterministic auto-repair so it stops
  occupying a retry slot.
- shipped         → fired on the FINAL draft (retry budget exhausted, flaw shipped via fallback).
- The corpus (evals/browser/orchestrator/cases.json) is built so decompose mostly SUCCEEDS on
  attempt 0, so retry transitions are sparse: `cases_with_retry` near 0 means we lack a
  retry-stress corpus to measure against, which is itself the finding.

Usage
-----
  uv run python scripts/validator_retry_efficacy.py                 # whole corpus
  uv run python scripts/validator_retry_efficacy.py --label WebArena
  uv run python scripts/validator_retry_efficacy.py --limit 5 --json out.json

Production decompose() is untouched: it gains an optional `attempt_observer` hook that is None
in every production call (zero behavior change); only this harness installs one.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import ValidationIssue, decompose
from gui_agent.core.orchestrator._validator.governance import (
    TEXTUAL_FALLBACK_HEURISTIC_SAMPLES,
    TEXTUAL_FALLBACK_VALIDATOR_CODES,
)
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge, load_knowledge_for_app

CASES_FILE = PROJECT_ROOT / "evals" / "browser" / "orchestrator" / "cases.json"


@dataclass
class CaseTrace:
    attempts: list[list[str]]
    error: str | None = None


def _case_knowledge(case: dict):
    platform = case.get("platform", "browser")
    app = case.get("knowledge_app") or case.get("site")
    if app:
        return load_knowledge_for_app(app, platform)
    return auto_discover_knowledge(case["goal"], platform)


def _decompose_with_trace(case: dict) -> CaseTrace:
    """Run one case through decompose, returning the per-attempt list of fired codes
    (attempts[0] = codes on the first draft, attempts[-1] = codes on the final draft).

    If decompose raises after the observer has seen validator attempts, keep those attempts. A
    compile failure is exactly the case where the final fired codes matter most; treating it as an
    empty trace makes the rule look unobserved and hides failed repairs.
    """
    attempts: list[list[str]] = []

    def observer(_attempt: int, issues: list[ValidationIssue]) -> None:
        attempts.append([i.code for i in issues])

    try:
        k = _case_knowledge(case)
        screenshot = case.get("screenshot")
        png_bytes = None
        if screenshot:
            p = PROJECT_ROOT / screenshot
            if not p.exists():
                return CaseTrace(attempts, error=f"missing screenshot fixture: {screenshot}")
            png_bytes = p.read_bytes()

        resolution = None
        if case.get("resolution"):
            from gui_agent.core.router import EntityRef, IntentResolution

            resolution = IntentResolution(entities=[EntityRef(**e) for e in case["resolution"]])

        decompose(
            case["goal"],
            png_bytes=png_bytes,
            knowledge=k.decompose_context(case["goal"]) if k else "",
            current_url=case.get("current_url", ""),
            current_title=case.get("current_title", ""),
            current_site=case.get("current_site")
            or (k.app_name if k and case.get("use_knowledge_app_as_current_site") else ""),
            table_summaries=case.get("table_summaries"),
            corrective_directive=case.get("corrective_directive", ""),
            resolution=resolution,
            attempt_observer=observer,
        )
    except Exception as exc:  # one bad case shouldn't sink the batch; preserve observed attempts
        return CaseTrace(attempts, error=f"{type(exc).__name__}: {exc}")
    return CaseTrace(attempts)


class CodeStat:
    __slots__ = ("fired", "fed_back", "cleared", "shipped")

    def __init__(self) -> None:
        self.fired = 0      # times the code appeared on any draft
        self.fed_back = 0   # times it appeared on a draft that HAD a following retry
        self.cleared = 0    # of those, gone on the next draft
        self.shipped = 0    # times it survived on the final (shipped) draft

    @property
    def clear_rate(self) -> float | None:
        return self.cleared / self.fed_back if self.fed_back else None


def aggregate(traces: list[list[list[str]]]) -> dict[str, CodeStat]:
    stats: dict[str, CodeStat] = defaultdict(CodeStat)
    for attempts in traces:
        if not attempts:
            continue
        last = len(attempts) - 1
        for i, codes in enumerate(attempts):
            next_codes = set(attempts[i + 1]) if i < last else None
            for c in codes:
                st = stats[c]
                st.fired += 1
                if i == last:
                    st.shipped += 1
                else:
                    st.fed_back += 1
                    if c not in next_codes:  # type: ignore[operator]
                        st.cleared += 1
    return stats


def _report(traces: list[CaseTrace], stats: dict[str, CodeStat]) -> None:
    total = len(traces)
    with_retry = sum(1 for t in traces if len(t.attempts) > 1)
    clean = sum(1 for t in traces if t.attempts and not t.attempts[0])
    failed = sum(1 for t in traces if t.error)
    print(f"\ncases: {total}   first-draft-clean: {clean}   needed≥1 retry: {with_retry}   raised: {failed}")
    if not stats:
        print("\nNo validation issues fired across the corpus — nothing to measure.")
        print("→ This corpus is built to pass on the first draft; to measure retry efficacy")
        print("  we need a retry-stress corpus (drafts that deliberately trip each rule).")
        return
    print("\nper-code retry efficacy (sorted: lowest clear-rate = most空转 / repair candidate):")
    print(f"  {'code':<38} {'fired':>5} {'fedback':>7} {'cleared':>7} {'rate':>6} {'shipped':>7}")
    rows = sorted(stats.items(), key=lambda kv: (kv[1].clear_rate is None, kv[1].clear_rate or 0, -kv[1].fired))
    for code, st in rows:
        rate = "  n/a" if st.clear_rate is None else f"{st.clear_rate * 100:4.0f}%"
        flag = ""
        if st.clear_rate is not None and st.clear_rate < 0.5:
            flag = "  ← low: reword msg or make deterministic-repair"
        elif st.shipped:
            flag = "  ← shipped with flaw"
        print(f"  {code:<38} {st.fired:>5} {st.fed_back:>7} {st.cleared:>7} {rate:>6} {st.shipped:>7}{flag}")
    watched_missing = sorted(code for code in TEXTUAL_FALLBACK_VALIDATOR_CODES if code not in stats)
    if watched_missing:
        print("\ntextual-fallback validator codes not observed in this run:")
        for code in watched_missing:
            print(f"  {code}  ← add retry-stress cases before trusting this rule's feedback")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="", help="substring filter on case label")
    ap.add_argument("--limit", type=int, default=0, help="cap number of cases (0 = all)")
    ap.add_argument("--json", default="", help="also write the raw per-case traces + stats here")
    args = ap.parse_args()

    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if args.label:
        cases = [c for c in cases if args.label.lower() in c["label"].lower()]
    if args.limit:
        cases = cases[: args.limit]

    traces: list[CaseTrace] = []
    for idx, c in enumerate(cases, 1):
        print(f"[{idx}/{len(cases)}] {c['label'][:70]}", flush=True)
        trace = _decompose_with_trace(c)
        if trace.error:
            print(f"    ! decompose raised: {trace.error}")
        traces.append(trace)

    stats = aggregate([trace.attempts for trace in traces])
    _report(traces, stats)

    if args.json:
        out = {
            "cases": [
                {"label": c["label"], "attempts": t.attempts, "error": t.error}
                for c, t in zip(cases, traces)
            ],
            "stats": {
                code: {"fired": s.fired, "fed_back": s.fed_back, "cleared": s.cleared,
                       "shipped": s.shipped, "clear_rate": s.clear_rate}
                for code, s in stats.items()
            },
            "textual_fallback_validator_codes": sorted(TEXTUAL_FALLBACK_VALIDATOR_CODES),
            "textual_fallback_heuristic_samples": sorted(
                str(sample.get("id") or "") for sample in TEXTUAL_FALLBACK_HEURISTIC_SAMPLES
            ),
        }
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
