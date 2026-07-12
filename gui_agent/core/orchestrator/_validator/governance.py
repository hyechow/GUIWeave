"""Governance metadata for validator rules that rely on text heuristics.

These are allowed as last-resort structural fallbacks, but they carry maintenance debt:
each must have a trigger sample in tests/test_validator_codes.py and should be reviewed with
scripts/validator_retry_efficacy.py after prompt/model changes. Keep this list small; prefer
schema fields, typed contracts, or deterministic predicates before adding here.
"""

from __future__ import annotations


TEXTUAL_FALLBACK_VALIDATOR_CODES: frozenset[str] = frozenset({
    # Uses textual "preserve/keep/追加" cues plus extracted entity tokens. Current stoplist includes
    # WebArena/Magento pressure words (pending/complete/reviews/status) until router exposes a
    # stronger scope-role contract.
    "PRESERVED_SCOPE_FILTER_MISSING_VALUE",
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
        # Live trigger (WebArena 502, run 20260708_183100): "Filters 面板的 Name 字段" extracted
        # ['面板的'] — `name` matched as designator and swallowed the real field "Name", making the
        # RETRIEVAL_RETRY_DROPS_FIELD feedback unsatisfiable across all 3 decompose retries.
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
    {
        "id": "orchestrator.arrival_submit_regex.show_report",
        "kind": "normalize_confirm_read_gate",
        "owner": "gui_agent.core.orchestrator.passes",
        "statements": [
            {"name": "进入 Reports > Sales > Orders 报表页", "kind": "navigation", "success_condition": "报表筛选页已显示"},
            {"name": "设置日期范围并点击 Show Report", "kind": "action", "success_condition": "统计表格已渲染出 N 行"},
        ],
        "expect_dispatch_gate": True,
        "expect_success_condition_contains": "纯导航/展示意图",
        "retire_when": "arrival-submit is represented by a structured action role",
    },
    {
        "id": "orchestrator.mutating_terminal_regex.save_not_arrival",
        "kind": "normalize_confirm_read_gate",
        "owner": "gui_agent.core.orchestrator.passes",
        "statements": [
            {"name": "进入 CMS Pages 列表页", "kind": "navigation", "success_condition": "页面显示 CMS Pages 列表"},
            {"name": "将 Page Title 字段更新为 {new_title} 并保存", "kind": "action", "success_condition": "页面显示保存成功提示"},
        ],
        "expect_dispatch_gate": False,
        "expect_success_condition": "页面显示保存成功提示",
        "retire_when": "mutating-vs-arrival terminal intent is represented by a structured action role",
    },
    {
        "id": "runtime.terminal_dispatch_regex.submit_comment",
        "kind": "terminal_dispatch_turn",
        "owner": "gui_agent.core.supervisor.milestone.policy",
        "trigger": "点击 Submit Comment 按钮",
        "expected": True,
        "retire_when": "terminal dispatch actions expose a structured dispatch role from the executor",
    },
    {
        "id": "runtime.terminal_dispatch_regex.checkbox_excluded",
        "kind": "terminal_dispatch_turn",
        "owner": "gui_agent.core.supervisor.milestone.policy",
        "trigger": "勾选 Notify Customer by Email 复选框",
        "expected": False,
        "retire_when": "terminal dispatch actions expose a structured dispatch role from the executor",
    },
    {
        "id": "runtime.negative_action_feedback_regex.validation_error",
        "kind": "negative_action_feedback",
        "owner": "gui_agent.core.supervisor.milestone.policy",
        "trigger": "validation failed: required field missing",
        "expected": True,
        "retire_when": "checker emits structured failure categories instead of free-text issues",
    },
    {
        "id": "runtime.negative_action_feedback_regex.required_column_is_not_error",
        "kind": "negative_action_feedback",
        "owner": "gui_agent.core.supervisor.milestone.policy",
        "trigger": "Attribute Code, Default Label, Required, System, Visible, Scope",
        "expected": False,
        "retire_when": "checker emits structured failure categories instead of free-text issues",
    },
    {
        "id": "runtime.preserve_scope_regex.keep_entity_keyword",
        "kind": "runtime_preserved_scope_filter",
        "owner": "gui_agent.core.supervisor.milestone.helpers",
        "milestone_name": "保留 Grace 客户结果范围，追加 Status=Pending 筛选",
        "milestone_success_condition": "可见筛选状态同时包含 Grace 客户范围和 Status: Pending",
        "applied_filters": {"Keyword": "Grace Nguyen", "Status": "Pending"},
        "expected": True,
        "retire_when": "milestones carry structured preserved_scope_filters",
    },
    {
        "id": "validator.preserve_scope_regex.missing_value",
        "kind": "validator_issue",
        "owner": "gui_agent.core.orchestrator.validator",
        "validator_code": "PRESERVED_SCOPE_FILTER_MISSING_VALUE",
        "trigger": "保留客户筛选结果范围，追加 Status=Pending",
        "retire_when": "router resolution marks preserved lookup scopes structurally",
    },
)
