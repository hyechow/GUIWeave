from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from gui_agent.core.runtime.traversal import TraversalSession, TraversalWindow
from gui_agent.core.schemas import StatementContract

from .schemas import _PlanResult

AcquireStatus = Literal["inactive", "ready", "act", "ambiguous", "exhausted"]

_CONTAINER_KINDS = ("section_toggle", "accordion", "tab", "treeitem")
_COLLAPSED_VALUES = {"0", "false", "no", "closed", "collapsed", "off", "hidden"}
_DEFAULT_SCROLL_BUDGET = 12


def _label(control: dict) -> str:
    return str(
        control.get("label")
        or control.get("name")
        or control.get("id")
        or control.get("placeholder")
        or ""
    ).strip()


def _tokens(value: str) -> tuple[str, ...]:
    """Canonicalize capability names without a platform/domain alias table."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    result: list[str] = []
    for token in re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")):
        token = token[:-1] if len(token) > 4 and token.endswith("s") else token
        if len(token) >= 3 and token not in result:
            result.append(token)
    return tuple(result)


def _aliases(control: dict) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(control.get(key) or "").strip()
            for key in ("label", "name", "id", "placeholder", "group_field", "key")
            if str(control.get(key) or "").strip()
        )
    )


def _match_score(query: str, control: dict) -> float:
    query_tokens = set(_tokens(query))
    if not query_tokens:
        return 0.0
    best = 0.0
    for alias in _aliases(control):
        alias_tokens = set(_tokens(alias))
        if not alias_tokens:
            continue
        if alias_tokens == query_tokens:
            return 1.0
        if alias_tokens <= query_tokens or query_tokens <= alias_tokens:
            overlap = len(alias_tokens & query_tokens) / len(alias_tokens | query_tokens)
            best = max(best, 0.8 + 0.1 * overlap)
    return best


def _is_container(control: dict) -> bool:
    kind = str(control.get("kind") or "").lower()
    return any(part in kind for part in _CONTAINER_KINDS)


def _direction(control: dict) -> Literal["up", "down"] | None:
    if control.get("in_viewport") is not False:
        return None
    position = control.get("viewport_pos")
    if position == "above":
        return "up"
    if position == "below":
        return "down"
    return None


def _y(control: dict) -> float | None:
    rect = control.get("rect")
    value = rect.get("y") if isinstance(rect, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _collapsed(control: dict) -> bool:
    value = str(control.get("selected_text") or control.get("value") or "").strip().lower()
    return value in _COLLAPSED_VALUES


@dataclass(frozen=True)
class AcquireDecision:
    status: AcquireStatus
    plan: _PlanResult | None = None
    target_labels: tuple[str, ...] = ()
    reason: str = ""


class TargetAcquireController:
    """Resolve and acquire a declared target before normal statement execution.

    The controller owns only the multi-turn positioning operation. Adapter structure is optional:
    when no unique target can be resolved it returns ``inactive`` and the visual planner remains
    authoritative. Desired values never participate in location binding.
    """

    def __init__(self, *, scroll_budget: int = _DEFAULT_SCROLL_BUDGET) -> None:
        self.scroll_budget = max(1, scroll_budget)
        self._traversals: dict[tuple[str, str], TraversalSession] = {}

    @staticmethod
    def _queries(statement: StatementContract) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in statement.target_controls or []
                if str(value or "").strip()
            )
        )

    @staticmethod
    def _resolve(controls: list[dict], queries: tuple[str, ...]) -> tuple[list[dict], str]:
        matches: list[dict] = []
        for query in queries:
            ranked = sorted(
                ((_match_score(query, control), control) for control in controls),
                key=lambda pair: pair[0],
                reverse=True,
            )
            score = ranked[0][0] if ranked else 0.0
            if score <= 0:
                continue
            best = [control for candidate, control in ranked if candidate == score]
            if len(best) != 1:
                return matches, f"semantic target {query!r} has {len(best)} equally strong matches"
            if best[0] not in matches:
                matches.append(best[0])
        return matches, ""

    @staticmethod
    def _scroll_target(matches: list[dict]) -> tuple[dict | None, str]:
        containers = [control for control in matches if _is_container(control)]
        fields = [control for control in matches if not _is_container(control)]
        visible_field = any(_direction(control) is None for control in fields)

        # A declared container below the viewport owns the search space. A container above may
        # already contain a visible descendant, in which case positioning is complete.
        candidates = [
            control
            for control in containers
            if _direction(control) == "down" or (_direction(control) == "up" and not visible_field)
        ]
        if not candidates:
            candidates = [control for control in fields if _direction(control) is not None]
        if not candidates:
            return None, ""
        directions = {_direction(control) for control in candidates}
        if len(directions) != 1:
            return None, "resolved targets require conflicting scroll directions"
        return candidates[0], ""

    def _traverse_target(
        self,
        *,
        scope: str,
        target: str,
        direction: Literal["up", "down"],
        position: float | None,
    ) -> tuple[str, str]:
        key = (scope, " ".join(_tokens(target)))
        session = self._traversals.get(key)
        if session is None:
            session = TraversalSession(
                f"target:{key[1]}",
                coverage="from_current",
                boundary_status="exhausted",
                no_progress_status="exhausted",
                max_moves=self.scroll_budget,
            )
            self._traversals[key] = session
        # As the page scrolls down, a below-fold target's document-relative y decreases. Negating
        # y gives the shared traversal model a monotonically increasing forward position.
        traversal_position = -position if position is not None else None
        window = TraversalWindow(
            surface_id=scope,
            position_key=f"target-y:{position}" if position is not None else "target-y:unknown",
            # Target positioning is geometric. The semantic target stays constant across valid
            # scrolls, so it must not be used as a window-content progress signal.
            content_key="",
            position=traversal_position,
            can_forward=direction == "down",
            can_backward=direction == "up",
        )
        decision = session.observe(window)
        if decision.action in {"exhausted", "ambiguous"}:
            self._traversals.pop(key, None)
        return decision.action, decision.reason

    def _clear_targets(self, scope: str, labels: tuple[str, ...]) -> None:
        for label in labels:
            self._traversals.pop((scope, " ".join(_tokens(label))), None)

    def decide(
        self,
        form_controls: Optional[list[dict]],
        statement: StatementContract,
        *,
        scope: str,
    ) -> AcquireDecision:
        queries = self._queries(statement)
        if statement.kind not in {"action", "filter"} or not queries:
            return AcquireDecision("inactive", reason="statement has no positioning target")
        controls = [
            control
            for control in form_controls or []
            if isinstance(control, dict) and _aliases(control)
        ]
        if not controls:
            return AcquireDecision("inactive", reason="adapter exposed no target structure")

        matches, ambiguity = self._resolve(controls, queries)
        labels = tuple(_label(control) for control in matches)
        if ambiguity:
            return AcquireDecision("ambiguous", target_labels=labels, reason=ambiguity)
        if not matches:
            return AcquireDecision("inactive", reason="no unique structural target match")

        target, conflict = self._scroll_target(matches)
        if conflict:
            return AcquireDecision("ambiguous", target_labels=labels, reason=conflict)
        if target is not None:
            label = _label(target)
            direction = _direction(target)
            assert direction is not None
            traversal_action, traversal_reason = self._traverse_target(
                scope=scope,
                target=label,
                direction=direction,
                position=_y(target),
            )
            if traversal_action == "exhausted":
                return AcquireDecision("exhausted", target_labels=labels, reason=traversal_reason)
            if traversal_action == "ambiguous":
                return AcquireDecision("ambiguous", target_labels=labels, reason=traversal_reason)
            direction_text = "向上" if direction == "up" else "向下"
            plan = _PlanResult(
                instruction=f"{direction_text}滚动到「{label}」附近",
                summary=(
                    f"AcquireTarget 已唯一绑定「{label}」，目标位于当前视口"
                    f"{'上方' if direction == 'up' else '下方'}；"
                    "继续同一目标定向过程。"
                ),
                atomic_role="iterate",
                action_family="iterate",
                target_control=label,
                direction=direction,
            )
            return AcquireDecision("act", plan=plan, target_labels=labels)

        # Positioning reached the declared container. Only an explicitly collapsed state is safe
        # to activate; unknown disclosure state falls through to visual execution.
        visible_fields = [
            control
            for control in matches
            if not _is_container(control) and _direction(control) is None
        ]
        collapsed = next(
            (
                control
                for control in matches
                if _is_container(control) and _collapsed(control)
            ),
            None,
        )
        if collapsed is not None and not visible_fields:
            label = _label(collapsed)
            self._clear_targets(scope, labels)
            plan = _PlanResult(
                instruction=f"点击或展开「{label}」区域",
                summary=(
                    f"AcquireTarget 已定位「{label}」，该区域明确处于折叠状态。"
                ),
                atomic_role="prepare",
                action_family="activate",
                target_control=label,
            )
            return AcquireDecision("act", plan=plan, target_labels=labels)

        self._clear_targets(scope, labels)
        return AcquireDecision(
            "ready",
            target_labels=labels,
            reason="target positioning is complete",
        )
