"""Adaptive, Journal-replayable executor for one explicit Acquire statement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gui_agent.core.orchestrator.program import Acquire
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.collection_view import (
    CollectionView,
    build_collection_view,
    collection_candidates,
    coverage_status,
    project_collection_slice,
)
from gui_agent.core.schemas import (
    AcquisitionReceiptEvent,
    EventJournal,
    StatementContract,
    StatementOutcome,
)

from .acquire_policy import AcquireDecision, decide_acquisition
from .observation import ObservationCursor


MAX_ACQUIRE_MOVES = 40
VISUAL_NO_PROGRESS_CONFIRMATIONS = 2
_FORWARD = {"paginate_next", "scroll_forward", "load_more"}
_ACTION_TYPES = {
    "paginate_next": {"tap"},
    "paginate_prev": {"tap"},
    "load_more": {"tap"},
    "scroll_forward": {"scroll", "drag"},
    "scroll_backward": {"scroll", "drag"},
}


@dataclass(frozen=True)
class AcquireMemoryView:
    """Pure replay projection; it deliberately has no phase or cursor."""

    bound_region: str
    failed_capabilities: frozenset[str]
    probed_capabilities: frozenset[str]
    receipts: tuple[AcquisitionReceiptEvent, ...]


def build_acquire_memory(
    journal: EventJournal, *, instance_id: str, statement_id: str,
) -> AcquireMemoryView:
    receipts = tuple(
        event for event in journal.acquisition_receipts
        if event.statement_instance_id == instance_id and event.statement_id == statement_id
    )
    slices = [
        event for event in journal.collection_slices
        if event.statement_instance_id == instance_id and event.statement_id == statement_id
    ]
    bound = next(
        (event.bound_region for event in reversed(receipts) if event.bound_region),
        slices[-1].provenance.surface_fingerprint if slices else "",
    )
    return AcquireMemoryView(
        bound_region=bound,
        failed_capabilities=frozenset(
            event.capability for event in receipts if event.status in {"failed", "rejected"}
        ),
        probed_capabilities=frozenset(event.capability for event in receipts),
        receipts=receipts,
    )


def _bound(candidates: list[dict[str, Any]], surface: str) -> dict | None:
    return next(
        (item for item in candidates if item["surface_fingerprint"] == surface), None,
    )


def _visual_boundary_confirmed(memory: AcquireMemoryView, view: CollectionView) -> bool:
    index = next(
        (
            i for i in range(len(memory.receipts) - 1, -1, -1)
            if memory.receipts[i].strategy == "react"
            and memory.receipts[i].capability == "visual_boundary"
        ),
        -1,
    )
    if index < 0:
        return False
    boundary = memory.receipts[index]
    if not view.collection_key or boundary.collection_key != view.collection_key:
        return False
    moves = [
        event for event in memory.receipts[:index]
        if event.strategy == "react" and event.status == "observed"
        and event.action_family in _FORWARD
        and event.collection_key == view.collection_key
        and event.bound_region == memory.bound_region
        and event.before_content_key == event.after_content_key
        and event.before_content_key
    ][-VISUAL_NO_PROGRESS_CONFIRMATIONS:]
    return (
        len(moves) == VISUAL_NO_PROGRESS_CONFIRMATIONS
        and all(event.after_content_key == boundary.before_content_key for event in moves)
    )


@dataclass
class _AcquireExecutor:
    invocation: StatementInvocation
    cursor: ObservationCursor
    bundle: Any
    platform: Any
    context: Any
    instance_id: str
    save_context: Any
    say: Any
    status: Any
    reports: list[dict] = field(default_factory=list)

    @property
    def statement(self) -> Acquire:
        assert isinstance(self.invocation.statement, Acquire)
        return self.invocation.statement

    @property
    def journal(self) -> EventJournal:
        return self.context.journal

    @property
    def contract(self) -> StatementContract:
        return StatementContract(
            id=self.statement.id,
            goal=self.statement.goal,
            success=self.statement.goal,
            returns=dict(self.statement.returns),
        )

    def memory(self) -> AcquireMemoryView:
        return build_acquire_memory(
            self.journal,
            instance_id=self.instance_id,
            statement_id=self.statement.id,
        )

    def attempts(self) -> int:
        return sum(
            event.action_family != "bind_region" for event in self.memory().receipts
        )

    def view(self) -> CollectionView:
        return build_collection_view(
            instance_id=self.instance_id,
            contract=self.contract,
            history=self.journal.events,
        )

    def receipt(
        self,
        strategy: str,
        capability: str,
        family: str,
        status: str,
        *,
        view: CollectionView | None = None,
        bound: str = "",
        before: str = "",
        after: str = "",
        reason: str = "",
    ) -> None:
        event = AcquisitionReceiptEvent(
            event_ref=f"acquire:{len(self.journal.acquisition_receipts) + 1}",
            after_turn=len(self.journal.turns),
            statement_instance_id=self.instance_id,
            statement_id=self.statement.id,
            collection_key=view.collection_key if view else "",
            bound_region=bound or self.memory().bound_region,
            strategy=strategy,  # type: ignore[arg-type]
            capability=capability,
            action_family=family,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            before_content_key=before,
            after_content_key=after,
            reason=reason,
        )
        self.journal.append_acquisition_receipt(event)
        self.save_context()

    def slice(self, candidate: dict, strategy: str):
        observation = self.cursor.observation
        assert observation is not None
        event = project_collection_slice(
            observation,
            self.contract,
            instance_id=self.instance_id,
            after_turn=len(self.journal.turns),
            event_ref=f"collection:{len(self.journal.collection_slices) + 1}",
            frame_ref=self.cursor.observation_url or "",
            table=candidate["table"],
            strategy=strategy,  # type: ignore[arg-type]
        )
        if event is None:
            return None
        duplicate = next(
            (
                old for old in reversed(self.journal.collection_slices)
                if old.statement_instance_id == self.instance_id
                and old.collection_key == event.collection_key
                and old.content_key == event.content_key
                and (
                    bool(event.window_key) and old.window_key == event.window_key
                    or bool(event.frame_ref) and old.frame_ref == event.frame_ref
                )
            ),
            None,
        )
        if duplicate is None:
            self.journal.append_collection_slice(event)
            self.save_context()
        return duplicate or event

    def completed(self, summary: str, verification: str = "confirmed") -> StatementOutcome:
        view = self.view()
        output = next(iter(self.statement.returns))
        return StatementOutcome.completed(
            summary,
            verification=verification,  # type: ignore[arg-type]
            outputs={output: list(view.records)},
            evidence=[segment.event_ref for segment in view.observed_segments],
            observation=self.cursor.observation,
            observation_url=self.cursor.observation_url,
            context_reports=self.reports,
        )

    def budget_outcome(self, max_moves: int) -> StatementOutcome:
        view = self.view()
        spec = next(iter(self.statement.returns.values()))
        if spec.coverage == "best_effort":
            return self.completed(
                f"采集预算耗尽，保留 {len(view.records)} 条部分记录",
                "accepted_unverified",
            )
        return StatementOutcome.exhausted(
            f"完整采集在 {max_moves} 次移动预算内未到达可信边界",
            observation=self.cursor.observation,
            observation_url=self.cursor.observation_url,
            context_reports=self.reports,
        )

    def policy_context(
        self, candidates: list[dict[str, Any]], memory: AcquireMemoryView, view: CollectionView,
    ) -> dict:
        spec = next(iter(self.statement.returns.values()))
        return {
            "goal": self.invocation.goal,
            "coverage": spec.coverage,
            "bound_region": memory.bound_region or None,
            "collection": {
                "collection_key": view.collection_key,
                "record_count": len(view.records),
                "known_total": view.known_total,
                "coverage_status": coverage_status(view),
                "last_move_result": view.last_move_result,
            },
            "candidates": [
                {key: item[key] for key in (
                    "ref", "surface_fingerprint", "caption", "headers",
                    "record_count", "reliable",
                )}
                for item in candidates
            ],
            "failed_capabilities": sorted(memory.failed_capabilities),
            "recent_receipts": [
                {
                    "strategy": event.strategy,
                    "capability": event.capability,
                    "action_family": event.action_family,
                    "status": event.status,
                    "content_changed": bool(
                        event.before_content_key and event.after_content_key
                        and event.before_content_key != event.after_content_key
                    ),
                    "reason": event.reason,
                }
                for event in memory.receipts[-6:]
            ],
        }

    def structured_move(self, candidate: dict, current: Any, index: int) -> bool:
        traversal = candidate["table"].get("traversal") or {}
        family = "paginate_next" if traversal.get("type") == "paged" else "scroll_forward"
        before = current.content_key if current else ""
        capability = f"structured:{candidate['surface_fingerprint']}:{before}:{family}"
        memory = self.memory()
        if capability in memory.probed_capabilities or self.bundle.move_collection is None:
            return False
        self.status(f"Acquire 结构化采集 {index + 1}/{MAX_ACQUIRE_MOVES}")
        if not self.bundle.move_collection(self.platform, candidate["table"], family):
            self.receipt(
                "structured", capability, family, "failed",
                view=self.view(), before=before,
                reason="adapter capability could not move the bound collection",
            )
            return False
        self.cursor.refresh(f"screenshot_acquire_{index + 2}.png")
        after_candidate = _bound(
            collection_candidates(self.cursor.observation), memory.bound_region,
        )
        after_event = self.slice(after_candidate, "structured") if after_candidate else None
        self.receipt(
            "structured", capability, family, "observed",
            view=self.view(), before=before,
            after=after_event.content_key if after_event else "",
            reason="adapter-bound move",
        )
        return True

    def physical_react(
        self,
        decision: AcquireDecision,
        candidate: dict,
        view: CollectionView,
        before: str,
        index: int,
    ) -> None:
        family = decision.action_family or "wait"
        capability = f"react:{self.memory().bound_region}:{before}:{family}"
        if capability in self.memory().failed_capabilities:
            raise RuntimeError("AcquirePolicy repeated a failed same-window capability")
        policy = self.bundle.make_action_policy(self.bundle.default_action_policy)
        action = policy.decide(
            self.cursor.observation,
            decision.instruction,
            action_family="iterate",
            target_control=("集合分页控件" if decision.target_role == "pager" else "绑定集合滚动区域"),
            expected_result="只暴露同一集合的下一窗口，不改变筛选或打开记录",
            context_reports=self.reports,
        )
        action_type = str(getattr(getattr(action, "action", None), "action_type", ""))
        valid = action_type in _ACTION_TYPES.get(family, set())
        if valid and self.bundle.validate_collection_action is not None:
            valid = self.bundle.validate_collection_action(
                self.platform, candidate["table"], action, family,
            )
        if not valid:
            self.receipt(
                "react", capability, family, "rejected", view=view, before=before,
                reason="action escaped acquisition whitelist or bound traversal affordance",
            )
            return
        dispatched = bool(
            self.bundle.make_executor(self.platform).execute(
                action,
                png_bytes=self.cursor.observation.png_bytes,
                target_control="集合分页或滚动控件",
            )
        )
        after_event = None
        if dispatched:
            self.cursor.refresh(f"screenshot_acquire_{index + 2}.png")
            candidate = _bound(
                collection_candidates(self.cursor.observation), self.memory().bound_region,
            )
            after_event = self.slice(candidate, "react") if candidate else None
        self.receipt(
            "react", capability, family, "observed" if dispatched else "failed",
            view=view, before=before,
            after=after_event.content_key if after_event else before,
            reason=decision.reason,
        )

    def react(
        self,
        decision: AcquireDecision,
        candidates: list[dict[str, Any]],
        memory: AcquireMemoryView,
        view: CollectionView,
        before: str,
        index: int,
    ) -> StatementOutcome | None:
        if not memory.bound_region:
            candidate = next(
                (item for item in candidates if item["ref"] == decision.bound_hint), None,
            )
            if decision.action_family != "bind_region" or candidate is None:
                return StatementOutcome.infeasible(
                    "AcquirePolicy 未从当前候选声明唯一绑定区域",
                    kickback="由上游 Interact 圈定集合，或让采集策略引用当前候选 ref",
                    context_reports=self.reports,
                )
            self.receipt(
                "react", f"bind:react:{candidate['ref']}", "bind_region", "selected",
                bound=candidate["surface_fingerprint"], reason=decision.reason,
            )
            return None
        if decision.kind == "blocked":
            return StatementOutcome.infeasible(
                decision.reason,
                kickback="Acquire 无法移动已绑定集合；由 Program 改路线或降级 coverage",
                context_reports=self.reports,
            )
        if decision.kind == "boundary":
            self.receipt(
                "react", "visual_boundary", "wait", "selected",
                view=view, before=before, after=before, reason=decision.reason,
            )
            return (
                self.completed(f"已在视觉边界确认采集 {len(view.records)} 条记录")
                if _visual_boundary_confirmed(self.memory(), view) else None
            )
        if decision.action_family == "wait":
            self.cursor.refresh(f"screenshot_acquire_{index + 2}.png")
            self.receipt(
                "react", f"react:{before}:wait", "wait", "observed",
                view=view, before=before, after=before, reason=decision.reason,
            )
            return None
        candidate = _bound(candidates, memory.bound_region)
        if candidate is None:
            return StatementOutcome.infeasible(
                "React fallback 丢失已绑定集合",
                kickback="回到上游 Interact 重新圈定集合",
                context_reports=self.reports,
            )
        try:
            self.physical_react(decision, candidate, view, before, index)
        except RuntimeError as exc:
            return StatementOutcome.exhausted(str(exc), context_reports=self.reports)
        return None

    def run(self, max_moves: int) -> StatementOutcome:
        check = self.invocation.args.get("source_check")
        if check is False or (isinstance(check, dict) and check.get("available") is False):
            return StatementOutcome.infeasible(
                "上游数据可用性检查未通过",
                kickback="用 Data inspect + Program If 处理缺列/集合未圈定，再进入 Acquire",
            )
        self.cursor.ensure(0)
        while True:
            observation = self.cursor.observation
            candidates = collection_candidates(observation)
            memory = self.memory()
            candidate = _bound(candidates, memory.bound_region) if memory.bound_region else None
            reliable = [item for item in candidates if item["reliable"]]
            if memory.bound_region and candidate is None:
                return StatementOutcome.infeasible(
                    "已绑定集合在当前帧消失或 provenance 漂移",
                    kickback="回到上游 Interact 重新圈定同一集合",
                )
            if not memory.bound_region and len(reliable) > 1:
                return StatementOutcome.infeasible(
                    "当前帧有多个可遍历结构集合，Acquire 不能猜测业务目标",
                    kickback="由上游 Interact 把目标范围收敛到唯一集合",
                )
            if not memory.bound_region and len(reliable) == 1:
                candidate = reliable[0]
                self.receipt(
                    "structured", "bind:structured", "bind_region", "selected",
                    bound=candidate["surface_fingerprint"], reason="唯一可靠结构集合",
                )
                memory = self.memory()

            current = self.slice(
                candidate, "structured" if candidate and candidate["reliable"] else "react",
            ) if candidate else None
            view = self.view()
            if (
                coverage_status(view) == "complete"
                and not any((view.provenance_drift, view.provenance_incomplete,
                             view.total_drift, view.truncated))
            ):
                return self.completed(f"已采集 {len(view.records)} 条记录")
            before = current.content_key if current else ""
            terminal_only = self.attempts() >= max_moves
            if not terminal_only and candidate and candidate["reliable"]:
                if self.structured_move(candidate, current, self.attempts()):
                    continue
                terminal_only = self.attempts() >= max_moves
            if not candidates:
                return StatementOutcome.infeasible(
                    "当前平台没有可物化的集合观察",
                    kickback="该平台需先实现 NormalizedObservation materializer",
                )
            memory = self.memory()
            decision = decide_acquisition(
                observation,
                self.policy_context(candidates, memory, view),
                context_reports=self.reports,
            )
            self.say(f"  [AcquirePolicy] {decision.kind}: {decision.reason}")
            if (
                terminal_only
                and decision.kind == "move"
                and not (
                    decision.action_family == "bind_region"
                    and not memory.bound_region
                )
            ):
                return self.budget_outcome(max_moves)
            outcome = self.react(
                decision, candidates, memory, view, before, self.attempts(),
            )
            if outcome is not None:
                return outcome
            if terminal_only and decision.kind == "boundary":
                return self.budget_outcome(max_moves)


def execute_acquire_statement(
    invocation: StatementInvocation,
    *,
    cursor: ObservationCursor,
    bundle: Any,
    platform: Any,
    context: Any,
    instance_id: str,
    save_context,
    say,
    status,
    max_moves: int = MAX_ACQUIRE_MOVES,
) -> StatementOutcome:
    statement = invocation.statement
    if not isinstance(statement, Acquire):
        raise TypeError("execute_acquire_statement requires Acquire")
    if len(statement.returns) != 1 or next(iter(statement.returns.values())).type != "list[record]":
        return StatementOutcome.failed("Acquire 必须且只能声明一个 list[record] output")
    return _AcquireExecutor(
        invocation, cursor, bundle, platform, context, instance_id,
        save_context, say, status,
    ).run(max_moves)


__all__ = [
    "AcquireMemoryView",
    "MAX_ACQUIRE_MOVES",
    "VISUAL_NO_PROGRESS_CONFIRMATIONS",
    "build_acquire_memory",
    "execute_acquire_statement",
]
