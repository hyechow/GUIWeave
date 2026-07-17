"""Issue types and registry for orchestrator program validation."""

from __future__ import annotations

# Severity is metadata for governance/measurement and decomposer gating. Only "error" blocks
# compile/retry; "warn" is advisory feedback that can be reported or measured without rejecting a
# structurally executable program.
_SEVERITIES = frozenset({"error", "warn"})


class ValidationIssue(str):
    """A single validator finding.

    It is its DSL-author-facing message (a `str` subclass) so the decompose retry loop can feed it
    straight back to the LLM and existing substring/`== []` assertions keep working unchanged —
    while carrying structured metadata (`code`, `severity`, `evidence`) so rules become countable,
    filterable, and individually testable.
    """

    code: str
    severity: str
    evidence: tuple

    def __new__(cls, code: str, message_for_llm: str, *, severity: str = "error", evidence=()) -> "ValidationIssue":
        if severity not in _SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        obj = super().__new__(cls, message_for_llm)
        obj.code = code
        obj.severity = severity
        obj.evidence = tuple(evidence)
        return obj

    @property
    def message_for_llm(self) -> str:
        return str(self)


class IssueList(list):
    """A `list[ValidationIssue]` with an `.add(code, message, ...)` collector.

    Subclasses `list`, so it stays compatible everywhere the old `list[str]` flowed (iteration,
    `== []`, prompt joining).
    """

    def add(self, code: str, message: str, *, severity: str = "error", evidence=()) -> None:
        self.append(ValidationIssue(code, message, severity=severity, evidence=evidence))

    @classmethod
    def one(cls, code: str, message: str, *, severity: str = "error", evidence=()) -> "IssueList":
        lst = cls()
        lst.add(code, message, severity=severity, evidence=evidence)
        return lst


# Canonical registry of every code validate_program can emit — the single source of truth that
# makes the rule set governable: tests assert (a) the codes emitted in validator.py == ALL_CODES
# (no drift), and (b) each code has at least one triggering sample (no dead rule). Add a code here
# in the same change that adds its `issues.add(...)` site.
ALL_CODES: frozenset[str] = frozenset({
    # program shape / result source
    "EMPTY_PROGRAM",
    "NO_RESULT_SOURCE",
    "MUTATE_GOAL_WITHOUT_ACTION",
    # {var[field]} template references (finish + result-then-reference run text)
    "TEMPLATE_VAR_NOT_IN_SCOPE",
    "TEMPLATE_FIELD_NOT_IN_RETURNS",
    "TEMPLATE_BARE_VAR",
    "TEMPLATE_UNSUPPORTED_EXPR",
    "COMPUTE_VAR_UNUSED",
    # per-run shape
    "PRECONDITION_NOT_NAVIGATION",
    "READ_MISSING_RETURNS",
    "READ_MISSING_VAR",
    "DATA_QUERY_MISSING_RETURNS",
    "DATA_QUERY_MISSING_VAR",
    "DATA_QUERY_MISSING_SQL",
    "DATA_QUERY_SQL_TEMPLATE_REF",
    "DATA_QUERY_VAR_AS_TABLE",
    "RETURNS_WITHOUT_READ_SPEC",
    "MUTATION_RESULT_UNUSED",
    "SPLIT_PERSISTENCE_BOUNDARY",
    "FILTER_RESULT_WITHOUT_TARGET_STATE",
    "DATA_QUERY_URL_RESULT_UNUSED",
    "DATA_QUERY_URL_ALIAS_NOT_URL_SOURCE",
    # function / capability data-flow
    "CALL_FUNC_NOT_DEFINED",
    "CALL_RETURNS_WITHOUT_VAR",
    "FUNCTION_RETURN_NOT_PRODUCED",
    "FUNCTION_URL_PARAM_NOT_USED",
    # table aggregation must go through data_query, not UI eyeballing
    "VISUAL_ROW_AGGREGATION",
    "TABLE_ROW_FIELD_COLLECTION",
    # data_query SQL hygiene
    "SQL_SCHEMA_MAPPING_TEXT",
    "SQL_QUOTED_DISPLAY_IDENTIFIER",
    "SQL_BARE_BOOLEAN_LITERAL",
    "RANK_QUERY_DROPS_TIES",
    "AGGREGATE_LIMIT_AFTER_AGGREGATION",
    "TEMPORAL_LIMIT_WITHOUT_ORDER",
    "TEMPORAL_AGGREGATE_WITHOUT_ROW_LIMIT",
    "SINGLE_TARGET_LIMIT_HIDES_AMBIGUITY",
    # interactive-step hygiene（statement=交互 action 的纪律）
    "NOOP_FLOW_CONTROL_STEP",
    # compute compile-time contract（编译期强制运行时方言与作用域）
    "COMPUTE_UNSUPPORTED_EXPR",
    "COMPUTE_UNKNOWN_NAME",
    # if-condition shape
    "IF_COND_VAR_NOT_IN_SCOPE",
    "IF_COND_FIELD_NOT_IN_RETURNS",
    "IF_COND_MISSING_VALUE",
    "IF_COND_MISSING_VALUES",
    "IF_EMPTY_GUARD_INVERTED",
    # foreach shape
    "FOREACH_OVER_NOT_IN_SCOPE",
    "FOREACH_MISSING_LOOP_VAR",
    "FOREACH_EMPTY_BODY_NO_RETURNS",
    "FOREACH_BODY_GOAL_MISSING_RETURNS",
    "FOREACH_BODY_GOAL_NO_ROW_TEMPLATE",
    "FOREACH_COLLECTION_UNUSED",
    "FOREACH_DETAIL_OPEN_NO_ROW_REFERENCE",
    "FOREACH_CALL_DROPS_ROW_URL",
    "FOREACH_ROW_URL_NOT_USED",
    # retrieval exact->fuzzy retry must keep the same field
    "RETRIEVAL_RETRY_DROPS_FIELD",
    # foreach / data_query field provenance
    "FOREACH_DQ_ROW_FIELD_MISSING",
    "FOREACH_DQ_UNKNOWN_TABLE",
    "FOREACH_DQ_GRID_FIELD_MISSING",
    "FOREACH_DQ_DETAIL_FIELD_MISSING",
    "FOREACH_DQ_POST_FOREACH_FIELD_MISSING",
    "FOREACH_BODY_GOAL_QUERY_ROW_PREDICATE",
    "EMAIL_RESULT_WITHOUT_EMAIL_SOURCE",
})
