"""Issue types for the semantic Program structural validator."""

from __future__ import annotations


class ValidationIssue(str):
    code: str
    severity: str
    evidence: tuple

    def __new__(
        cls,
        code: str,
        message_for_llm: str,
        *,
        severity: str = "error",
        evidence=(),
    ) -> "ValidationIssue":
        if severity not in {"error", "warn"}:
            raise ValueError(f"unknown severity {severity!r}")
        issue = super().__new__(cls, message_for_llm)
        issue.code = code
        issue.severity = severity
        issue.evidence = tuple(evidence)
        return issue

    @property
    def message_for_llm(self) -> str:
        return str(self)


class IssueList(list[ValidationIssue]):
    def add(self, code: str, message: str, *, severity: str = "error", evidence=()) -> None:
        self.append(ValidationIssue(code, message, severity=severity, evidence=evidence))


ALL_CODES = frozenset(
    {
        "EMPTY_PROGRAM",
        "DUPLICATE_STATEMENT_ID",
        "EMPTY_STATEMENT_GOAL",
        "INTERACT_MISSING_SUCCESS",
        "ACQUIRE_OUTPUT_CONTRACT",
        "ACQUIRE_COVERAGE_REQUIRED",
        "ACQUIRE_RAW_FIELDS_FORBIDDEN",
        "ACQUIRE_SOURCE_CHECK_REQUIRED",
        "ACQUIRE_SOURCE_CHECK_INVALID",
        "SOURCE_CHECK_OUTPUT_CONTRACT",
        "SOURCE_CHECK_FIELDS_REQUIRED",
        "COMPUTE_CHAIN_NOT_FUSED",
        "READ_INPUT_FORBIDDEN",
        "READ_COVERAGE_INVALID",
        "READ_BINDINGS_INVALID",
        "READ_OUTPUT_REQUIRED",
        "READ_RECORD_FIELDS_REQUIRED",
        "COMPUTE_REQUIRED_FIELDS_REQUIRED",
        "COMPUTE_FIELDS_NOT_ACQUIRED",
        "COMPUTE_SOURCE_INPUT_REQUIRED",
        "COMPUTE_OUTPUT_CONTRACT",
        "RETURNS_WITHOUT_BIND",
        "BIND_REDEFINED",
        "REF_NOT_IN_SCOPE",
        "REF_FIELD_NOT_DECLARED",
        "COMMAND_MISSING_ARGUMENT",
        "COMMAND_ARGUMENT_DUPLICATE",
        "COMMAND_ARGUMENT_INVALID",
        "COMMAND_OUTPUT_UNSUPPORTED",
        "FOREACH_ITEMS_NOT_IN_SCOPE",
        "FOREACH_ITEMS_NOT_LIST",
        "FOREACH_COLLECT_NOT_IN_SCOPE",
        "COVERAGE_REQUIRES_LIST_RECORD",
        "ROUTER_LOOKUP_NOT_DECLARED",
    }
)


__all__ = ["ALL_CODES", "IssueList", "ValidationIssue"]
