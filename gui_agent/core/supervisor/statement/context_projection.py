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
_MAX_PROJECTED_GROUPS = 4
_WRITE_ROLES = {
    "combobox",
    "input",
    "listbox",
    "native_select",
    "select",
    "text_input",
    "textbox",
}


def _expected_state_signals(statement: StatementContract) -> list[str]:
    signals = [str(key) for key in statement.expected_state]
    for value in statement.expected_state.values():
        values = value if isinstance(value, list) else [value]
        signals.extend(
            str(item)
            for item in values
            if isinstance(item, (str, int, float, bool))
        )
    return signals


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
    intent_signals = [
        statement.goal,
        statement.success,
        *_expected_state_signals(statement),
    ]
    if statement.interaction_intent is not None:
        intent_signals.append(
            _PHASE_KNOWLEDGE_HINTS[statement.interaction_intent.phase]
        )
        intent_signals.extend([
            statement.interaction_intent.entity,
            *statement.interaction_intent.required_fields,
        ])
    intent_stems = knowledge.match_signals(
        intent_signals,
        min_overlap=2,
        match_titles=False,
    )
    if statement.expected_state or (
        statement.interaction_intent is not None
        and statement.interaction_intent.phase == "reach"
    ):
        return intent_stems[:3]
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
    declared_container_offscreen = any(
        item.get("in_viewport") is False
        and _declared_control_field(statement, item)
        for item in complete
    )
    selected = []
    grouped: dict[str, list[dict]] = {}
    for item in bounded:
        kind = str(item.get("kind") or item.get("type") or "").strip().casefold()
        if not any(
            token in kind
            for token in (
                "input", "select", "text", "checkbox", "radio", "switch",
                "combobox", "listbox",
            )
        ):
            continue
        group_id = str(item.get("group_id") or "").strip()
        if group_id:
            if _declared_control_field(statement, item):
                grouped.setdefault(group_id, []).append(item)
            continue
        if declared_container_offscreen and not _declared_control_field(statement, item):
            continue
        selected.append(item)
    if not grouped:
        return selected

    desired = {
        _normalized(field): value
        for field, value in statement.required_values.items()
    }

    def group_score(entry: tuple[int, list[dict]]) -> tuple[int, int, int, int]:
        order, controls = entry
        values = [
            control.get("selected_text", control.get("value"))
            for control in controls
        ]
        indexes = [
            int(value)
            for control in controls
            if str(value := control.get("group_index") or "").isdigit()
        ]
        matched = sum(
            field in desired and _normalized(value) == _normalized(desired[field])
            for control, value in zip(controls, values)
            if (field := _declared_control_field(statement, control))
        )
        return matched, int(all(value in (None, "") for value in values)), max(indexes, default=-1), order

    ranked = sorted(
        enumerate(grouped.values()),
        key=group_score,
        reverse=True,
    )
    return [
        *selected,
        *[
            item
            for _, controls in ranked[:_MAX_PROJECTED_GROUPS]
            for item in controls
        ],
    ]


def _declared_control_field(
    statement: StatementContract,
    item: dict,
) -> str:
    declared = {
        _normalized(field)
        for field in (*statement.required_values, *statement.observe_fields)
        if _normalized(field)
    }
    if not declared:
        return ""
    label = str(item.get("label") or "").strip()
    group_field = str(item.get("group_field") or "").strip()
    identities = {
        _normalized(label),
        _normalized(group_field),
        _normalized(f"{group_field} {label}"),
    }
    matches = declared & identities
    return next(iter(matches)) if len(matches) == 1 else ""


def resolve_required_write_ref(
    statement: StatementContract,
    observation: Observation,
    *,
    target_control: str,
    target_value: str,
) -> str:
    """Bind one declared write to the leading projected form unit."""
    target = _normalized(target_control)
    fields = [
        _normalized(field)
        for field, value in statement.required_values.items()
        if _normalized(value) == _normalized(target_value)
        and _normalized(field) in target
    ]
    if len(fields) != 1:
        return ""
    controls = _decision_controls(statement, observation)
    subject = next(
        (
            str(control.get("group_id") or "")
            for control in controls
            if control.get("group_id")
        ),
        "",
    )
    candidates = [
        control
        for control in controls
        if _declared_control_field(statement, control) == fields[0]
        and (
            str(control.get("group_id") or "") == subject
            if subject
            else not control.get("group_id")
        )
    ]
    if len(candidates) != 1:
        return ""
    control = candidates[0]
    return str(
        control.get("ref")
        or control.get("id")
        or control.get("name")
        or ""
    ).strip()


def _compact_form_units(controls: list[dict]) -> list[dict]:
    units: dict[str, dict] = {}
    for item in controls:
        unit_id = str(item.get("group_id") or "__form__").strip()
        unit = units.setdefault(unit_id, {
            "id": unit_id,
            "index": item.get("group_index"),
            "fields": [],
        })
        label = str(item.get("label") or "").strip()
        group_field = str(item.get("group_field") or "").strip()
        field = " ".join(part for part in (group_field, label) if part)
        ref = str(
            item.get("ref")
            or item.get("id")
            or item.get("name")
            or ""
        ).strip()
        unit["fields"].append({
            "field": field or label,
            "kind": item.get("kind") or item.get("type"),
            "ref": ref,
            "value": item.get(
                "selected_text",
                item.get("value", item.get("checked")),
            ),
            "in_viewport": item.get("in_viewport"),
        })
    return list(units.values())


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
    if statement.required_values:
        source = tuple(
            item
            for item in source
            if item.get("role") not in _WRITE_ROLES
        )
    keys = (
        "label",
        "ref",
        "role",
        "visibility",
        "supported_operations",
        "group_id",
        "group_index",
        "group_field",
        "value",
        "is_filter",
        "query_action",
        "form_action",
    )
    projected = [
        {
            **{key: item[key] for key in keys if key in item},
            "_relevance": _affordance_relevance(statement, item),
        }
        for item in source
    ]
    priority = {
        "contract_target": 0,
        "current": 1,
        "supporting": 2,
        "background": 3,
    }
    return sorted(
        projected,
        key=lambda item: priority.get(item["_relevance"], 3),
    )


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


def declared_target_affordances(
    statement: StatementContract,
    view: StatementObservationView,
) -> tuple[dict, ...]:
    """Return affordances structurally associated with top-level contract fields."""
    return tuple(
        item
        for item in view.affordances
        if _matches_contract_field(statement, item.get("label"))
    )


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
        terms.extend(intent.predicates)

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
    """Project visible rows only; offscreen tables remain document-level identities."""
    tables = observation.tables or []
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
    projected: list[dict] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        visibility = (
            "visible"
            if table.get("in_viewport") is True
            else "offscreen"
            if table.get("in_viewport") is False
            else "unknown"
        )
        if statement.interaction_intent is None and visibility == "visible":
            item = dict(table)
        else:
            item = {key: table[key] for key in keys if key in table}
        item["visibility"] = visibility
        if table.get("viewport_pos") in {"above", "below"}:
            item["viewport_pos"] = table["viewport_pos"]
        projected.append(item)
    return projected


def project_transition_observation(
    statement: StatementContract,
    observation: Observation,
    view: StatementObservationView,
    *,
    initial_filters: dict[str, str] | None,
) -> dict:
    """Project the current observation for this Transition consumer."""
    controls = _decision_controls(statement, observation)
    affordances = _compact_affordances(statement, view)
    return {
        "title": observation.title,
        "url": observation.url,
        "affordance_coverage": view.affordance_coverage,
        "declared_targets": [
            {
                key: value
                for key, value in item.items()
                if key != "_relevance"
            }
            for item in affordances
            if item.get("_relevance") == "contract_target"
        ],
        "form_units": _compact_form_units(controls),
        "applied_filters": observation.applied_filters or {},
        "initial_filters": initial_filters or {},
        "tables": _project_tables(statement, observation),
        "affordances": affordances,
    }


def _last_action_delivery(memory: StatementMemoryView) -> dict:
    for fact in reversed(memory.durable_facts):
        if fact.kind != "action_receipt":
            continue
        return {
            "event_ref": fact.event_ref,
            "role": fact.metadata.get("role") or "",
            "response": fact.metadata.get("response") or "unknown",
        }
    return {"status": "none"}


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
        "handoff": memory.previous_statement or {"status": "none"},
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
            "last_action_delivery": _last_action_delivery(memory),
        },
        "observation": project_transition_observation(
            statement,
            observation,
            view,
            initial_filters=initial_filters,
        ),
    }


__all__ = [
    "declared_target_affordances",
    "project_transition_frame",
    "project_transition_observation",
    "resolve_required_write_ref",
    "select_transition_knowledge",
]
