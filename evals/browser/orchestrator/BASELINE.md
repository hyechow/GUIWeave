# Browser orchestrator baseline — hard single-site

## Scope

Official WebArena-Verified **hard** subset, **pure single-site only**:

| Site | Cases | Source |
|------|------:|--------|
| shopping_admin | 55 | hard subset ∩ `sites==[shopping_admin]` |
| shopping | 56 | hard subset ∩ `sites==[shopping]` |
| **Total** | **111** | |

Not included: multi-site hard, non-hard, live browser / WebArena oracle scores.

## Scoring (only two bars)

| Bar | Applies to | Pass means |
|-----|------------|------------|
| **executable** | all 111 | `validate_code` clean + `plan.executable` |
| **curated contract** | 14 hand-written admin cases | executable **and** AST `contract` match |

Everything else on a case is **annotation for reuse**, not a score:

- non-curated `contract` with `contract_origin: "baseline_inferred"` — frozen shape of the baseline program
- `baseline.grade` / `failure_codes`

| Grade | Who | Meaning |
|-------|-----|---------|
| `executable_pass` | non-curated | executable ok |
| `executable_fail` | any | not executable |
| `contract_pass` | curated | executable + contract ok |
| `contract_fail` | curated | executable but contract miss |

`sample.ok` / exit code follow those bars only (inferred contracts never flip `ok`).

## Baseline run

| Field | Value |
|-------|--------|
| Run id | `20260805_hard_single_site_merged` |
| Date | 2026-08-05 |
| Model | `qwen3.7-plus` |
| Provider | `tokenplan` |
| Concurrency | `-j 5` |
| Report | `logs/orchestrator_eval/20260805_hard_single_site_merged/report.json` |
| Index | `baseline_qwen37_tokenplan_20260805.json` |
| Cases | `cases.json` (`baseline` + `contract` + `contract_origin`) |

### Headline

| Metric | Result |
|--------|--------|
| **executable** | **91 / 111 (82.0%)** |
| **curated contract** | **12 / 14 (85.7%)** |
| samples ok (same as executable∪curated rules) | 89 / 111 |
| shopping_admin executable | 45 / 55 |
| shopping executable | 46 / 56 |

### By grade

| Grade | N |
|-------|--:|
| `executable_pass` | 77 |
| `executable_fail` | 20 |
| `contract_pass` | 12 |
| `contract_fail` | 2 |

### Contract origins

| Origin | N | Role |
|--------|--:|------|
| `curated` | 14 | **scored** |
| `baseline_inferred` | 97 | annotated from baseline source; **not scored** |

### Curated failures

| task | grade | codes |
|-----:|-------|-------|
| 42 | contract_fail | `SLICE_STOP` |
| 549 | contract_fail | `ORDERED_CALL`, `LITERAL_REQUIRED` |

### Executable failures (ids)

- admin: 204, 505, 550, 551, 769, 771, 774, 776, 778, 782  
- shopping: 21, 286, 431, 435, 507, 508, 529, 530, 795, 798  

### Feasibility

- **Feasible** as a hard-single-site orchestration **smoke + regression** net: ~82% executable, curated 12/14.
- **Not** live WebArena success. Shopping still has no `knowledge/browser/shopping/` pack.
- Inferred contracts let you **diff shape drift** later without raising the score bar (see `annotation_failures` on samples).

## Reuse / re-verify

```bash
# full suite vs baseline
uv run python evals/browser/orchestrator/test_orchestrator.py -j 5 --compare-baseline

# primary bars only
uv run python evals/browser/orchestrator/test_orchestrator.py --group curated --compare-baseline
uv run python evals/browser/orchestrator/test_orchestrator.py --group admin -j 5

# list with grades / origins
uv run python evals/browser/orchestrator/test_orchestrator.py --list | head

# deterministic contract unit tests (no LLM)
uv run pytest tests/test_orchestrator_eval_cases.py -q
```

## Case schema

```json
{
  "task_id": 64,
  "group": "hard_shopping_admin",
  "site": "shopping_admin",
  "intent": "...",
  "contract": { "method_counts": { "commit": 0 }, "required_calls": [ ... ] },
  "contract_origin": "baseline_inferred",
  "curated": false,
  "baseline": {
    "run": "20260805_hard_single_site_merged",
    "date": "2026-08-05",
    "model": "qwen3.7-plus",
    "provider": "tokenplan",
    "ok": true,
    "executable": true,
    "grade": "executable_pass",
    "failure_codes": []
  }
}
```

Curated cases set `"curated": true`, `"contract_origin": "curated"`, and keep the hand-written contract.

Do **not** promote inferred contracts into the scored bar without human review; do **not** add compile semantic gates to chase executable fails (`docs/orchestrator_module_boundaries.md` §13.1).
