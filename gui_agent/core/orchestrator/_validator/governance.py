"""Governance metadata for validator rules that rely on text heuristics.

These are allowed as last-resort structural fallbacks, but they carry maintenance debt:
each must have a trigger sample in tests/test_validator_codes.py and should be reviewed with
scripts/validator_retry_efficacy.py after prompt/model changes. Keep this list small; prefer
schema fields, typed contracts, or deterministic predicates before adding here.
"""

from __future__ import annotations


TEXTUAL_FALLBACK_VALIDATOR_CODES: frozenset[str] = frozenset({
    # Uses textual search/filter retry cues and field-name extraction stopwords. Kept as a validator
    # issue so retry efficacy can reveal whether the feedback actually repairs drafts.
    "RETRIEVAL_RETRY_DROPS_FIELD",
})


# Textual heuristics that are not all validator issues still need a visible trigger registry.
# Tests execute these samples against the owning helper/pass so a future regex/stopword edit cannot
# silently widen the rule without adding a concrete regression example and a retirement note.
TEXTUAL_FALLBACK_HEURISTIC_SAMPLES: tuple[dict[str, object], ...] = (
    {
        "id": "retrieval.stopword.top_search_box_location",
        "kind": "retrieval_field_extract",
        "owner": "gui_agent.core.orchestrator._validator.retrieval",
        "trigger": "在顶部搜索框输入精确值『Grace Nguyen』进行筛选",
        "expected": [],
        "validator_code": "RETRIEVAL_RETRY_DROPS_FIELD",
        "retire_when": "retrieval target fields are typed in the DSL instead of inferred from prose",
    },
    {
        "id": "retrieval.stopword.bottom_search_box_location",
        "kind": "retrieval_field_extract",
        "owner": "gui_agent.core.orchestrator._validator.retrieval",
        "trigger": "在底部搜索框输入关键词『Grace』并提交",
        "expected": [],
        "validator_code": "RETRIEVAL_RETRY_DROPS_FIELD",
        "retire_when": "retrieval target fields are typed in the DSL instead of inferred from prose",
    },
    {
        # A container prefix before a field label must not be extracted as the field itself.
        "id": "retrieval.field_regex.name_column_literal",
        "kind": "retrieval_field_extract",
        "owner": "gui_agent.core.orchestrator._validator.retrieval",
        "trigger": "清除残留筛选，在 Filters 面板的 Name 字段用精确值『Gobi HeatTec Tee』筛选",
        "expected": ["name"],
        "validator_code": "RETRIEVAL_RETRY_DROPS_FIELD",
        "retire_when": "retrieval target fields are typed in the DSL instead of inferred from prose",
    },
    {
        # Same run: the fallback step said 「在同一 Name 字段用关键词重筛」 but the same-target
        # escape only accepted 同一 directly followed by a designator, so the explicit
        # same-field declaration did not count.
        "id": "retrieval.same_target.field_name_between",
        "kind": "retrieval_same_target",
        "owner": "gui_agent.core.orchestrator._validator.retrieval",
        "trigger": "清除精确值后在同一 Name 字段用关键词『HeatTec』重筛并提交",
        "expected": True,
        "validator_code": "RETRIEVAL_RETRY_DROPS_FIELD",
        "retire_when": "exact/fuzzy retries carry a structured target_field",
    },
    {
        "id": "retrieval.stopword.input_exact_overcapture",
        "kind": "retrieval_field_normalize",
        "owner": "gui_agent.core.orchestrator._validator.retrieval",
        "trigger": "输入精确客户",
        "expected": "",
        "validator_code": "RETRIEVAL_RETRY_DROPS_FIELD",
        "retire_when": "exact/fuzzy retries carry a structured target_field",
    },
    {
        "id": "retrieval.stopword.trailing_location_suffix",
        "kind": "retrieval_field_normalize",
        "owner": "gui_agent.core.orchestrator._validator.retrieval",
        "trigger": "产品顶部",
        "expected": "",
        "validator_code": "RETRIEVAL_RETRY_DROPS_FIELD",
        "retire_when": "field extraction is replaced by a structured parser or typed target_field",
    },
)
