"""Platform-neutral execution-scope and route-identity helpers."""

from __future__ import annotations

import re

from gui_agent.core.run.progress_monitor import canonical_url
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn
from gui_agent.core.self_learning.progressive import _norm as _norm_page


_UNKNOWN_PAGE_MARKERS = (
    "未知", "未识别", "无法识别", "不确定", "unknown", "unidentified"
)
_URL_TEXT_RE = re.compile(r"https?://[^\s「」'\"<>]+")
_IDENTITY_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def page_known(page_identity: str) -> bool:
    normalized = _norm_page(page_identity)
    return bool(normalized) and not any(
        marker in normalized for marker in _UNKNOWN_PAGE_MARKERS
    )


def resource_identity_from_url(url: str | None) -> str:
    """Return a resource identity from a detail-style URL without domain vocabulary."""
    path = canonical_url(url)
    if not path:
        return ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    lower = [part.lower() for part in parts]
    for index, part in enumerate(lower[:-1]):
        if (part == "id" or part.endswith("_id")) and parts[index + 1]:
            return "/".join(parts[: index + 2]).lower()
    detail_like = any(
        part in {"edit", "view", "detail", "details", "show"}
        for part in lower
    )
    if detail_like:
        for index in range(len(parts) - 1, -1, -1):
            token = parts[index]
            if any(ch.isdigit() for ch in token):
                return "/".join(parts[: index + 1]).lower()
    return ""


def resource_identity_from_text(text: str) -> str:
    for match in _URL_TEXT_RE.finditer(text or ""):
        identity = resource_identity_from_url(match.group(0))
        if identity:
            return identity
    return ""


def route_identity_evidence(milestone: Milestone, observation: Observation) -> str:
    """Describe a route identity only when the milestone names the same machine token."""
    identity = resource_identity_from_url(getattr(observation, "url", None))
    if not identity:
        return ""
    milestone_text = (
        f"{milestone.name}\n{milestone.description}\n{milestone.success_condition}"
    )
    declared = {
        token.casefold()
        for token in _IDENTITY_TOKEN_RE.findall(milestone_text)
        if any(ch.isdigit() for ch in token)
    }
    route_tokens = {
        token.casefold()
        for token in _IDENTITY_TOKEN_RE.findall(identity)
        if any(ch.isdigit() for ch in token)
    }
    matched = sorted(declared & route_tokens)
    if not matched:
        return ""
    return (
        "系统机器状态补充：当前资源路由与子目标共享标识 "
        f"{', '.join(matched[:3])}。该信号只确认当前资源身份；"
        "资源字段值仍应从当前观察读取并独立判断。"
    )


def execution_scope_for(milestone: Milestone, observation: Observation) -> str:
    """Bucket runtime memory by observable resource identity or milestone."""
    identity = resource_identity_from_url(getattr(observation, "url", None))
    if not identity:
        identity = resource_identity_from_text(
            f"{milestone.name}\n{milestone.description}\n{milestone.success_condition}"
        )
    if identity:
        return f"row:{identity}"
    return f"milestone:{milestone.id}"


def turn_execution_scope(turn: PolicyTurn) -> str:
    supervisor = getattr(turn, "supervisor", None)
    return str(getattr(supervisor, "execution_scope", "") or "") if supervisor else ""


def history_for_scope(
    history: list[PolicyTurn],
    milestone: Milestone,
    observation: Observation,
) -> list[PolicyTurn]:
    scope = execution_scope_for(milestone, observation)
    if any(turn_execution_scope(turn) for turn in history):
        return [turn for turn in history if turn_execution_scope(turn) == scope]
    return [
        turn
        for turn in history
        if getattr(getattr(turn, "supervisor", None), "milestone_id", None)
        == milestone.id
    ]


def history_for_current_milestone(
    history: list[PolicyTurn],
    milestone: Milestone,
    observation: Observation,
) -> list[PolicyTurn]:
    return [
        turn
        for turn in history_for_scope(history, milestone, observation)
        if getattr(getattr(turn, "supervisor", None), "milestone_id", None)
        == milestone.id
    ]


# Compatibility aliases for existing private imports.
_page_known = page_known
_resource_identity_from_url = resource_identity_from_url
_resource_identity_from_text = resource_identity_from_text
_turn_execution_scope = turn_execution_scope


__all__ = [
    "execution_scope_for",
    "history_for_current_milestone",
    "history_for_scope",
    "page_known",
    "resource_identity_from_text",
    "resource_identity_from_url",
    "route_identity_evidence",
    "turn_execution_scope",
]
