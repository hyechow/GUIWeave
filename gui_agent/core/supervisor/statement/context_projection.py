"""Pure, deterministic context projection for one Statement Transition.

Projection selects semantically relevant source facts before prompt assembly. It does not
summarize, truncate, enforce a size budget, call an LLM, or decide Statement progress.
"""

from __future__ import annotations

from gui_agent.core.run.statement_memory import StatementMemoryView
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge

from .observation_view import StatementObservationView


_PHASE_KNOWLEDGE_HINTS = {
    "reach": "reach navigate collection view",
    "locate": "locate collection fields schema",
    "constrain": "constrain filter collection grid view",
}


def select_transition_knowledge(
    statement: StatementContract,
    observation: Observation,
    knowledge: ProgressiveKnowledge,
) -> list[str]:
    """Select page knowledge from independent route and contract signals."""
    route_stems = knowledge.match_signals(
        [str(observation.title or ""), str(observation.url or "")],
        match_whens=False,
    )
    intent_signals = [statement.goal, statement.success]
    if statement.interaction_intent is not None:
        intent_signals.append(
            _PHASE_KNOWLEDGE_HINTS[statement.interaction_intent.phase]
        )
    intent_stems = knowledge.match_signals(
        intent_signals,
        min_overlap=2,
        match_titles=False,
    )
    return list(dict.fromkeys([*route_stems, *intent_stems]))[:3]


def _decision_controls(
    statement: StatementContract,
    observation: Observation,
) -> list[dict]:
    """Keep value state without copying the complete Runtime action index."""
    complete = [
        item
        for item in (observation.form_control_state or observation.form_controls or [])
        if isinstance(item, dict)
    ]
    intent = statement.interaction_intent
    if intent is not None and intent.phase in {"reach", "locate"}:
        return []
    if intent is not None and intent.phase == "constrain":
        return [
            item
            for item in complete
            if item.get("is_filter") is True or item.get("query_action")
        ]

    bounded = [
        item
        for item in (observation.form_controls or complete)
        if isinstance(item, dict)
    ]
    selected = []
    for item in bounded:
        kind = str(item.get("kind") or item.get("type") or "").strip().casefold()
        if item.get("group_id") or not any(
            token in kind
            for token in (
                "input", "select", "text", "checkbox", "radio", "switch",
                "combobox", "listbox",
            )
        ):
            continue
        selected.append(item)
    return selected


def _compact_control_state(
    statement: StatementContract,
    observation: Observation,
) -> list[dict]:
    keys = (
        "kind",
        "label",
        "name",
        "id",
        "value",
        "selected_text",
        "checked",
        "required",
        "in_viewport",
        "viewport_pos",
        "group_id",
        "group_index",
        "group_field",
        "options",
        "is_filter",
        "query_action",
    )
    return [
        {key: item[key] for key in keys if key in item}
        for item in _decision_controls(statement, observation)
    ]


def _compact_affordances(
    statement: StatementContract,
    view: StatementObservationView,
) -> list[dict]:
    intent = statement.interaction_intent
    source = (
        tuple(
            item
            for item in view.affordances
            if item.get("is_filter") is True or item.get("query_action")
        )
        if intent is not None and intent.phase == "constrain"
        else view.affordances
    )
    keys = (
        "label",
        "ref",
        "role",
        "visibility",
        "supported_operations",
        "is_filter",
        "query_action",
    )
    return [
        {
            **{key: item[key] for key in keys if key in item},
            "_relevance": _affordance_relevance(statement, item),
        }
        for item in source
    ]


def _affordance_relevance(
    statement: StatementContract,
    item: dict,
) -> str:
    """Classify affordance value for later capacity-driven compression."""
    if item.get("visibility") != "offscreen":
        return "current"
    if _matches_contract_field(statement, item.get("label")):
        return "contract_target"
    if item.get("query_action"):
        return "supporting"
    return "background"


def _matches_contract_field(
    statement: StatementContract,
    label: object,
) -> bool:
    terms: list[object] = [*statement.observe_fields]
    terms.extend(statement.required_values)
    terms.extend(statement.expected_state)
    intent = statement.interaction_intent
    if intent is not None:
        terms.extend((intent.entity, *intent.required_fields))
        terms.extend(predicate.field for predicate in intent.predicates)

    label_key = _normalized(label)
    return bool(label_key) and any(
        (term_key := _normalized(term))
        and (term_key in label_key or label_key in term_key)
        for term in terms
    )


def _normalized(value: object) -> str:
    return "".join(
        char.casefold()
        for char in str(value or "")
        if char.isalnum()
    )


def _project_tables(
    statement: StatementContract,
    observation: Observation,
) -> list[dict]:
    """Keep collection identity during typed UI phases; raw rows belong to Acquire."""
    tables = observation.tables or []
    if statement.interaction_intent is None:
        return tables
    keys = (
        "index",
        "source",
        "caption",
        "headers",
        "row_count",
        "dom_row_count",
        "total_records",
        "partial",
        "page",
        "traversal",
    )
    return [
        {key: table[key] for key in keys if key in table}
        for table in tables
        if isinstance(table, dict)
    ]


def project_transition_observation(
    statement: StatementContract,
    observation: Observation,
    view: StatementObservationView,
    *,
    initial_filters: dict[str, str] | None,
) -> dict:
    """Project the current observation for this Transition consumer."""
    return {
        "title": observation.title,
        "url": observation.url,
        "affordance_coverage": view.affordance_coverage,
        "control_state": _compact_control_state(statement, observation),
        "applied_filters": observation.applied_filters or {},
        "initial_filters": initial_filters or {},
        "tables": _project_tables(statement, observation),
        "affordances": _compact_affordances(statement, view),
    }


def _last_action_result(memory: StatementMemoryView) -> str:
    if not memory.recent_steps and not memory.durable_facts:
        return "none"
    if memory.recent_steps and "no_effect" in memory.recent_steps[-1].text:
        return "no_effect"
    for fact in reversed(memory.durable_facts):
        if fact.kind != "action_receipt":
            continue
        response = str(fact.metadata.get("response") or "")
        if response == "none_observed":
            return "no_effect"
        if response == "observed":
            return "effective"
        return "unknown"
    return "unknown"


def project_transition_frame(
    statement: StatementContract,
    observation: Observation,
    memory: StatementMemoryView,
    view: StatementObservationView,
    *,
    initial_filters: dict[str, str] | None,
) -> dict:
    """Build the canonical decision packet before size compression."""
    return {
        "contract": statement.model_dump(mode="json", exclude_none=True),
        "memory": {
            "instance_id": memory.instance_id,
            "durable_facts": [
                {
                    "kind": fact.kind,
                    "event_ref": fact.event_ref,
                    "text": fact.text,
                    "metadata": fact.metadata,
                }
                for fact in memory.durable_facts
            ],
            "recent_steps": [
                {"event_ref": step.event_ref, "text": step.text}
                for step in memory.recent_steps
            ],
            "compressed_history": list(memory.compressed_history),
            "last_action_result": _last_action_result(memory),
        },
        "observation": project_transition_observation(
            statement,
            observation,
            view,
            initial_filters=initial_filters,
        ),
    }


__all__ = [
    "project_transition_frame",
    "project_transition_observation",
    "select_transition_knowledge",
]
