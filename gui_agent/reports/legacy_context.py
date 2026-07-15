"""Offline adapter: migrate historical context.json shapes for report/replay only.

Runtime ``load_context`` must NOT call this module. New runs use strict schemas.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def adapt_legacy_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow-copied context dict safe for report builders.

    - Maps old Supervisor terminal fields into ``outcome`` when missing.
    - Leaves ``milestones`` / ``milestone_states`` for dual-read reducers.
    - Does not attempt to restore executable runtime state.
    """
    ctx = deepcopy(raw)
    turns = ctx.get("turns")
    if not isinstance(turns, list):
        return ctx
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        sv = turn.get("supervisor")
        if not isinstance(sv, dict):
            continue
        if sv.get("outcome") is not None:
            continue
        # Map legacy goal_completed/stop/replan_directive → StatementOutcome-like dict
        if sv.get("replan_directive"):
            sv["outcome"] = {
                "phase": "infeasible",
                "summary": str(sv.get("summary") or sv.get("stop_reason") or ""),
                "kickback": str(sv.get("replan_directive") or ""),
            }
        elif sv.get("goal_completed"):
            status = str(sv.get("completion_status") or "confirmed")
            verification = (
                "accepted_unverified"
                if status == "accepted_unverified"
                else "confirmed"
            )
            sv["outcome"] = {
                "phase": "completed",
                "summary": str(sv.get("summary") or ""),
                "verification": verification,
            }
        elif sv.get("stop"):
            sv["outcome"] = {
                "phase": "failed",
                "summary": str(sv.get("stop_reason") or sv.get("summary") or ""),
            }
        # Drop forbidden legacy keys so strict validators do not see them on re-parse
        for key in (
            "goal_completed",
            "stop",
            "stop_reason",
            "completion_status",
            "replan_directive",
        ):
            sv.pop(key, None)
    return ctx
