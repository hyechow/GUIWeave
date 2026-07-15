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
    done_check: dict = field(default_factory=dict)
    pre_existing: bool = False
    collection_summary: str = ""
    retry_count: int = 0
    last_summary: str = ""
    turn_indices: list[int] = field(default_factory=list)


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


def fold_checklist_from_checker(
    *,
    success_condition: str,
    fallback: str,
    checker: dict,
    items: dict[str, ChecklistItemView] | None = None,
) -> dict[str, ChecklistItemView]:
    """Fold one checker dict into checklist items (pure; no context writes)."""
    items = items if items is not None else {}
    status = str(checker.get("status") or "")
    reason = str(checker.get("reason") or "")
    visible = [str(v) for v in (checker.get("visible_evidence") or []) if str(v)]
    missing = [str(v) for v in (checker.get("missing_evidence") or []) if str(v)]

    if status == "done":
        item_status = "done"
    elif status == "stuck":
        item_status = "blocked"
    else:
        item_status = "pending"

    evidence = visible or ([reason] if reason else [])
    verdicts: dict[int, dict] = {}
    for v in (checker.get("item_verdicts") or []):
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
            source_i = "checker:item_verdict"
        else:
            item_status_i = item_status
            item_evidence_i = evidence
            source_i = "checker:success_condition"
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
            source="checker:missing_evidence",
        )
    return items


class StatementReportReducer:
    """Reduce journal turns into per-invocation statement views."""

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

        for turn in events or []:
            if not isinstance(turn, dict):
                continue
            sv = turn.get("supervisor") if isinstance(turn.get("supervisor"), dict) else {}
            instance_id = str(turn.get("statement_instance_id") or "")
            if not instance_id:
                continue
            mid = str(sv.get("milestone_id") or "")
            view = ensure(instance_id, mid)
            idx = turn.get("index")
            if isinstance(idx, int):
                view.turn_indices.append(idx)

            info = turn.get("statement")
            if isinstance(info, dict):
                view.statement_id = str(info.get("id") or view.statement_id)
                view.name = str(info.get("name") or view.name)
                view.description = str(info.get("description") or view.description)
                view.kind = str(info.get("kind") or view.kind)
                view.success_condition = str(
                    info.get("success_condition") or view.success_condition
                )

            if not view.name:
                view.name = str(sv.get("summary") or view.statement_id)
            if not view.kind:
                view.kind = str(sv.get("milestone_kind") or "")

            if sv.get("summary"):
                view.last_summary = str(sv["summary"])
            if sv.get("pre_existing"):
                view.pre_existing = True
            if sv.get("collection_summary"):
                view.collection_summary = str(sv["collection_summary"])

            outcome = sv.get("outcome") if isinstance(sv.get("outcome"), dict) else None
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

            checker = turn.get("checker")
            if isinstance(checker, dict) and checker:
                view.done_check = dict(checker)
                cmap = checklist_maps.setdefault(instance_id, {})
                fold_checklist_from_checker(
                    success_condition=view.success_condition,
                    fallback=view.name or view.statement_id,
                    checker=checker,
                    items=cmap,
                )

            if turn.get("replan"):
                view.retry_count += 1

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
