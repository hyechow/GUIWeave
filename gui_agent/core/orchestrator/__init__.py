"""DSL orchestrator — a compiler + runtime for the mixed script a GUI task decomposes into.

GUI task = script generation (see docs/milestone_as_function.md). The user's goal compiles to a
small program of interactive actions (FFI calls into the nondeterministic GUI executor) and
non-interactive statements (deterministic read/query/compute the interpreter runs itself). This
package is that compiler + runtime; each module has one role in the toolchain:

  ── language (AST + docs) ────────────────────────────────────────────────────────────
  program.py        the IR: Run (interactive action) / Read·Query (non-interactive) / If /
                    ForEach / Compute / Call / Finish. Wire-stable; the LLM draft is separate.
  prompts/…/decomposer.md   the language documentation (worked examples > rule prose).

  ── compiler frontend (AOT, LLM) ─────────────────────────────────────────────────────
  decomposer.py     NL goal → draft → AST. Three entrances share ONE pipeline (_invoke_plan):
                    decompose (AOT) / redecompose (kickback hot-patch) / subdecompose (per-row
                    JIT). Pipeline = LLM → to_program (structural passes) → validate/retry →
                    finalize_gates. So lint + gate-normalization cover all three uniformly.
  _decomposer/      private draft schema, context blocks, and SQL normalizations used by the
                    frontend; the public names are re-exported through decomposer.py.

  ── compiler middle-end (deterministic) ──────────────────────────────────────────────
  passes.py         AST normalize passes: collapse_foreach / insert_loop_entry_arrivals /
                    chain_from_states (structural, pre-validate) + finalize_gates (confirm-read
                    dispatch gate + precondition ensure-state gate, post-validate).

  ── type-check / lint (deterministic) ────────────────────────────────────────────────
  validator.py      reference/SQL/branch validation (type check). preflight.py    router-
  preflight.py      coverage + execution-mode discipline (lint); sample-and-validate uses it.
  _validator/       private validator rule families and issue registry; the public names are
                    re-exported through validator.py.

  ── non-interactive standard library (deterministic) ─────────────────────────────────
  primitives/       restricted SQL, compute expressions, URL-JSON reads, and vision field
                    extraction.
  traversal/        foreach row-collection traversal controller/runtime.

  ── runtime ──────────────────────────────────────────────────────────────────────────
  runner.py         the interpreter: non-interactive statements run here; interactive actions
                    yield out to the agent loop. budget.py  turn-cost estimate.

  ── FFI boundary (the milestone-as-function call ABI) ────────────────────────────────
  callframe.py      marshalling (to_milestone/package_result) + the call convention: return
                    contract, bounded recovery, typed kickback exception. See its docstring.
  recovery.py       exception-system Stage A: the four-class recovery taxonomy + the task-wide
                    RecoveryLedger every recovery mechanism reports to (record-only for now;
                    Stage B derives global budgets/escalation from the trace).

  Retired: engine.py (S9a) — its passes went to passes.py, its marshalling to callframe.py.
"""

from .program import (
    Call, Compute, Cond, CondCmp, Finish, ForEach, FunctionDef, If,
    INTERACTIVE_KINDS, NON_INTERACTIVE_KINDS, Program, Query, Read, Run, RunLike,
    RunResult, Stmt, execution_mode_for_kind,
)
from .decomposer import OrchestratorCompileError, decompose, redecompose, to_program
from .validator import IssueList, ValidationIssue, validate_program
from .intent_contracts import IntentContractIssue, validate_intent_contracts
from .preflight import (
    OrchestrationPreflightIssue,
    OrchestrationPreflightResult,
    validate_orchestration_preflight,
)
from .primitives.structured_read import structured_read
from .primitives.data_query import DataQueryError, execute_data_query
from .budget import estimate_program_turns
from .runner import (
    Interpreter,
    MilestoneExecutor,
    OrchestratorResult,
    ProgramRunner,
    RunRecord,
    drive,
    summarize_progress,
)
from .passes import (
    chain_from_states,
    collapse_foreach_enrichment_passes,
    finalize_gates,
    insert_loop_entry_arrivals,
    normalize_confirm_read_gates,
    normalize_precondition_gates,
)

__all__ = [
    "Call", "Compute", "Cond", "CondCmp", "Finish", "ForEach", "FunctionDef", "If",
    "INTERACTIVE_KINDS", "NON_INTERACTIVE_KINDS", "Program", "Query", "Read", "Run",
    "RunLike", "RunResult", "Stmt", "execution_mode_for_kind",
    "Interpreter", "MilestoneExecutor", "OrchestratorResult", "ProgramRunner",
    "RunRecord", "drive", "summarize_progress", "structured_read", "DataQueryError", "execute_data_query",
    "decompose", "redecompose", "to_program", "validate_program", "OrchestratorCompileError",
    "ValidationIssue", "IssueList", "IntentContractIssue", "validate_intent_contracts",
    "OrchestrationPreflightIssue", "OrchestrationPreflightResult", "validate_orchestration_preflight",
    "estimate_program_turns",
    "normalize_confirm_read_gates", "normalize_precondition_gates", "chain_from_states",
    "collapse_foreach_enrichment_passes", "insert_loop_entry_arrivals", "finalize_gates",
]
