"""Pure per-statement report projection from the persisted turn journal."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from gui_agent.core.schemas import split_acceptance_items


def _checklist_item_id(prefix: str, text: str) -> str:
    normalized = re.sub(r"\s+", "", text.strip().lower())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{digest}"


@dataclass
class ChecklistItemView:
    id: str
    text: str
    status: str = "pending"
    evidence: list[str] = field(default_factory=list)
    source: str = ""


@dataclass
class StatementView:
    """One statement invocation as rendered in the HTML report."""

    instance_id: str
    statement_id: str
    name: str = ""
    description: str = ""
    kind: str = ""
    success_condition: str = ""
    status: str = ""  # done | failed | running
    phase: str = ""
    verification: str = ""
    kickback: str = ""
    reads: dict[str, str] = field(default_factory=dict)
    checklist: list[dict] = field(default_factory=list)
    acceptance: dict = field(default_factory=dict)
    pre_existing: bool = False
    collection_summary: str = ""
    last_summary: str = ""
    turn_indices: list[int] = field(default_factory=list)
    verification_url: str = ""
    outcome_after_turn: int = 0
    outcome_timings: dict[str, float] = field(default_factory=dict)
    outcome_token_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    outcome_context: list[dict] = field(default_factory=list)


def _upsert_checklist(
    items: dict[str, ChecklistItemView],
    *,
    item_id: str,
    text: str,
    status: str,
    evidence: list[str] | None = None,
    source: str,
) -> None:
    clean = [e for e in (evidence or []) if e]
    existing = items.get(item_id)
    if existing is None:
        items[item_id] = ChecklistItemView(
            id=item_id, text=text, status=status, evidence=clean, source=source,
        )
        return
    existing.text = text
    if existing.status == "done" and status != "done":
        status = "done"
    existing.status = status
    if clean:
        existing.evidence = clean
    existing.source = source


def fold_checklist_from_verdict(
    *,
    success_condition: str,
    fallback: str,
    verdict: dict,
    items: dict[str, ChecklistItemView] | None = None,
) -> dict[str, ChecklistItemView]:
    """Fold one Outcome/Transition verdict into checklist items."""
    items = items if items is not None else {}
    status = str(verdict.get("status") or "")
    reason = str(verdict.get("reason") or "")
    visible = [str(v) for v in (verdict.get("visible_evidence") or []) if str(v)]
    missing = [str(v) for v in (verdict.get("missing_evidence") or []) if str(v)]

    if status == "done":
        item_status = "done"
    elif status == "stuck":
        item_status = "blocked"
    else:
        item_status = "pending"

    evidence = visible or ([reason] if reason else [])
    verdicts: dict[int, dict] = {}
    for v in (verdict.get("item_verdicts") or []):
        if isinstance(v, dict) and isinstance(v.get("index"), int):
            verdicts[v["index"]] = v

    for idx, text in enumerate(
        split_acceptance_items(success_condition, fallback), 1
    ):
        v = verdicts.get(idx)
        if v is not None:
            met = bool(v.get("met"))
            item_status_i = "done" if met else (
                "blocked" if status == "stuck" else "pending"
            )
            ev = str(v.get("evidence") or "").strip()
            item_evidence_i = [ev] if ev else (evidence if not met else [])
            source_i = "verdict:item"
        else:
            item_status_i = item_status
            item_evidence_i = evidence
            source_i = "verdict:success_condition"
        _upsert_checklist(
            items,
            item_id=_checklist_item_id("accept", text),
            text=text,
            status=item_status_i,
            evidence=item_evidence_i,
            source=source_i,
        )

    missing_status = "blocked" if status == "stuck" else "pending"
    for text in missing[:8]:
        _upsert_checklist(
            items,
            item_id=_checklist_item_id("missing", text),
            text=text,
            status=missing_status,
            evidence=[reason] if reason else [],
            source="verdict:missing_evidence",
        )
    return items


class StatementReportReducer:
    """Reduce turn and statement-outcome events into per-invocation views."""

    def reduce(
        self,
        *,
        events: list[dict],
    ) -> list[StatementView]:
        views: dict[str, StatementView] = {}
        order: list[str] = []
        checklist_maps: dict[str, dict] = {}

        def ensure(instance_id: str, statement_id: str = "") -> StatementView:
            if instance_id not in views:
                views[instance_id] = StatementView(
                    instance_id=instance_id,
                    statement_id=statement_id or instance_id,
                )
                order.append(instance_id)
            view = views[instance_id]
            if statement_id and not view.statement_id:
                view.statement_id = statement_id
            return view

        for event in events or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "turn")
            is_outcome = event_type == "statement_outcome"
            sv = (
                event.get("supervisor")
                if isinstance(event.get("supervisor"), dict)
                else {}
            )
            instance_id = str(event.get("statement_instance_id") or "")
            if not instance_id:
                continue
            mid = str(
                event.get("statement_id")
                if is_outcome
                else sv.get("statement_id")
                or ""
            )
            view = ensure(instance_id, mid)
            idx = event.get("index")
            if event_type == "turn" and isinstance(idx, int):
                view.turn_indices.append(idx)

            info = event.get("statement")
            if isinstance(info, dict):
                view.statement_id = str(info.get("id") or view.statement_id)
                view.name = str(info.get("name") or view.name)
                view.description = str(info.get("description") or view.description)
                view.kind = str(info.get("kind") or view.kind)
                view.success_condition = str(
                    info.get("success_condition") or view.success_condition
                )

            if not view.name:
                view.name = str(
                    sv.get("summary")
                    or (
                        (event.get("outcome") or {}).get("summary")
                        if is_outcome and isinstance(event.get("outcome"), dict)
                        else ""
                    )
                    or view.statement_id
                )
            if not view.kind:
                view.kind = str(
                    event.get("statement_kind")
                    if is_outcome
                    else sv.get("statement_kind")
                    or ""
                )

            if not is_outcome and sv.get("summary"):
                view.last_summary = str(sv["summary"])
            if (
                event.get("pre_existing")
                if is_outcome
                else sv.get("pre_existing")
            ):
                view.pre_existing = True
            collection_summary = (
                event.get("collection_summary")
                if is_outcome
                else sv.get("collection_summary")
            )
            if collection_summary:
                view.collection_summary = str(collection_summary)

            outcome = (
                event.get("outcome")
                if is_outcome and isinstance(event.get("outcome"), dict)
                else None
            )
            if outcome:
                view.phase = str(outcome.get("phase") or view.phase)
                view.verification = str(outcome.get("verification") or view.verification)
                view.kickback = str(outcome.get("kickback") or view.kickback)
                reads = outcome.get("reads")
                if isinstance(reads, dict) and reads:
                    view.reads = {str(k): str(v) for k, v in reads.items()}
                if view.phase == "completed":
                    view.status = "done"
                elif view.phase in {"failed", "exhausted", "infeasible", "interrupted"}:
                    view.status = "failed"
                view.last_summary = str(
                    outcome.get("summary") or view.last_summary
                )
                view.verification_url = str(
                    event.get("observation_url") or view.verification_url
                )
                view.outcome_after_turn = int(
                    event.get("after_turn") or view.outcome_after_turn
                )
                view.outcome_timings = dict(event.get("timings") or {})
                view.outcome_token_usage = dict(event.get("token_usage") or {})
                view.outcome_context = list(event.get("llm_context") or [])

                # A structural Guard may complete before invoking Transition. Reports still
                # need an acceptance projection, but it is derived from the terminal Outcome
                # instead of resurrecting a mutable completion state.
                terminal_check = {
                    "status": "done" if view.phase == "completed" else "stuck",
                    "reason": str(outcome.get("summary") or ""),
                    "summary": str(outcome.get("summary") or ""),
                    "effect_status": (
                        "confirmed" if view.phase == "completed" else "unverified"
                    ),
                    "visible_evidence": [
                        str(item)
                        for item in (outcome.get("evidence") or [])
                        if str(item)
                    ],
                }
                view.acceptance = terminal_check
                fold_checklist_from_verdict(
                    success_condition=view.success_condition,
                    fallback=view.name or view.statement_id,
                    verdict=terminal_check,
                    items=checklist_maps.setdefault(instance_id, {}),
                )

            transition = event.get("transition")
            proposal = (
                transition.get("proposal")
                if isinstance(transition, dict)
                and isinstance(transition.get("proposal"), dict)
                else None
            )
            if proposal:
                kind = str(proposal.get("kind") or "")
                evidence = [
                    str(item.get("claim") or "")
                    for item in (proposal.get("evidence") or [])
                    if isinstance(item, dict) and str(item.get("claim") or "")
                ]
                transition_check = {
                    "status": (
                        "done" if kind == "complete"
                        else "stuck" if kind == "infeasible"
                        else "in_progress"
                    ),
                    "reason": str(proposal.get("reason") or ""),
                    "summary": str(
                        (proposal.get("assessment") or {}).get("summary")
                        or proposal.get("summary")
                        or ""
                    ),
                    "effect_status": "confirmed" if kind == "complete" else "unverified",
                    "visible_evidence": evidence,
                    "missing_evidence": [
                        str(transition.get("validation_error") or "")
                    ] if transition.get("validation_error") else [],
                }
                if kind in {"complete", "infeasible"}:
                    view.acceptance = transition_check
                fold_checklist_from_verdict(
                    success_condition=view.success_condition,
                    fallback=view.name or view.statement_id,
                    verdict=transition_check,
                    items=checklist_maps.setdefault(instance_id, {}),
                )

        for view in views.values():
            cm = checklist_maps.get(view.instance_id) or {}
            view.checklist = [
                {
                    "id": item.id,
                    "text": item.text,
                    "status": item.status,
                    "evidence": list(item.evidence),
                    "source": item.source,
                }
                for item in cm.values()
            ]
            if not view.status:
                view.status = "running"

        return [views[i] for i in order]
