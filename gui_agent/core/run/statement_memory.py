"""StatementMemoryView — read-only decision context projected from EventJournal turns.

Normative authority (Agentic Statement Transition):

- EventJournal: fact authority (raw append-only events)
- StatementMemoryView: decision context for the LLM (this module)
- LLM Transition: semantic control; beliefs must not be promoted here
- Runtime validation: mechanical boundary only (not owned here)
- StatementOutcome: sole terminal

This module never stores live phase and never invents facts. Compaction may window
ordinary page prose and old routine acts; durable facts listed below are never dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from gui_agent.core.schemas import (
    Observation,
    PolicyTurn,
    StatementContract,
    target_value_options,
)

# Bounded narrative windows. Durable receipt/effect facts remain outside these limits.
DEFAULT_RECENT_K = 6
DEFAULT_COMPRESSED_K = 8


@dataclass(frozen=True)
class DurableFact:
    """A fact that must survive compaction (journal-backed, not LLM belief)."""

    kind: str
    text: str
    event_ref: str = ""  # e.g. "turn:12"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecentStep:
    """One recent journal turn summarized for the LLM window."""

    event_ref: str
    text: str


@dataclass(frozen=True)
class StatementMemoryView:
    """Frozen decision context for one statement instance.

    No ``phase`` / ``subphase`` fields — business transfer is not a state machine here.
    """

    instance_id: str
    statement_id: str
    contract_lines: tuple[str, ...]
    contract_requirements: tuple[str, ...]
    durable_facts: tuple[DurableFact, ...]
    recent_steps: tuple[RecentStep, ...]
    compressed_history: tuple[str, ...]
    previous_statement: dict[str, str] | None = None
    # The most recent dispatched action's stated expected result: the in-flight
    # sub-goal the agent is mid-way through. Rendered above the contract so the
    # decision continues a multi-step flow instead of reverting to the top-level
    # goal after an intermediate action (e.g. pressing Home to read an SMS code).
    pending_result: str = ""
    # The supervisor's most recent declared open gap — the pending fill/return
    # step (narrative memory, not completion evidence). Keeps a read value's
    # destination anchored after an external read.
    pending_gap: str = ""

    def render_prompt_section(self) -> str:
        """Render the bounded Journal projection consumed by the unified Transition."""
        lines = [
            "## StatementMemory（本调用事实记忆 · 非相位状态机）",
            "",
            "以下内容来自 EventJournal 投影，不是模型推断。"
            "LLM 推断不得升级为事实；完成与否须有合同与证据。",
            "",
        ]
        pending_lines: list[str] = []
        if self.pending_result:
            pending_lines.append(f"- 最近动作预期结果：{self.pending_result}")
        if self.pending_gap:
            pending_lines.append(f"- 待完成子步骤：{self.pending_gap}")
        if pending_lines:
            lines.append("### 当前进行中的子目标")
            lines.extend(pending_lines)
            lines.append("")
        lines.append("### 合同")
        lines.extend(f"- {line}" for line in self.contract_lines)
        if self.contract_requirements:
            lines.append("### 合同要求")
            lines.extend(f"- {item}" for item in self.contract_requirements)
        if self.durable_facts:
            lines.append("### 不可压缩事实（永久保留）")
            for fact in self.durable_facts:
                ref = f" [{fact.event_ref}]" if fact.event_ref else ""
                lines.append(f"- ({fact.kind}){ref} {fact.text}")
        if self.compressed_history:
            lines.append("### 更早步骤摘要（叙事上下文，不可作为终态证据引用）")
            lines.extend(f"- {row}" for row in self.compressed_history)
        if self.recent_steps:
            lines.append("### 最近步骤（叙事上下文；只有上方不可压缩事实可按 event_ref 引证）")
            for step in self.recent_steps:
                lines.append(f"- [{step.event_ref}] {step.text}")
        if not self.durable_facts and not self.recent_steps and not self.compressed_history:
            lines.append("### 历史")
            lines.append("- （本调用尚无 journal 步骤）")
        return "\n".join(lines)


def _event_ref(turn: PolicyTurn) -> str:
    return f"turn:{turn.index}"


def _instruction(turn: PolicyTurn) -> str:
    if turn.supervisor is None:
        return ""
    intent = turn.supervisor.action_intent
    return intent.instruction.strip() if intent is not None else ""


def _expected_result(turn: PolicyTurn) -> str:
    if turn.supervisor is None:
        return ""
    intent = turn.supervisor.action_intent
    return (intent.expected_result or "").strip() if intent is not None else ""


def _pending_gap(turn: PolicyTurn) -> str:
    """The last declared open gap (the LLM's own next-step directive).

    The assessment's ``open_gaps`` are the supervisor's declared unfinished
    steps — e.g. "尚未将验证码 910988 填入淘店登录表单". Carrying the most recent
    one forward (as narrative memory, not completion evidence) keeps a
    multi-step flow's fill/return step anchored after an external read.
    """
    transition = turn.transition or {}
    proposal = transition.get("proposal") or {}
    assessment = proposal.get("assessment") or {}
    gaps = assessment.get("open_gaps") or []
    for gap in gaps:
        text = str(gap or "").strip()
        if text:
            return text
    return ""


def _read_code(turn: PolicyTurn) -> str:
    """Return the verification code observed this turn, if any.

    Two sources, both perception/anchor facts (neither invents a code):
    1. ``turn.read_code`` — extracted from the observation semantic tree at
       turn-recording time (deterministic; the code lives in the SMS text node).
    2. ``turn.supervisor.summary`` — the supervisor's narrative. Zero extra cost
       (the summary is already produced), so it acts as a fallback when the code
       was seen but the semantic tree did not carry it.
    """
    if turn.read_code:
        return turn.read_code
    from gui_agent.core.run.turns import extract_code_from_text

    return extract_code_from_text(turn.supervisor.summary or "")


def _role(turn: PolicyTurn) -> str:
    if turn.supervisor is None:
        return ""
    intent = turn.supervisor.action_intent
    return intent.role if intent is not None else ""


def _contract_lines(contract: StatementContract) -> list[str]:
    lines = [
        f"目标：{contract.goal}",
    ]
    if contract.success:
        lines.append(f"验收条件：{contract.success}")
    if contract.expected_state:
        payload = json.dumps(
            contract.expected_state,
            ensure_ascii=False,
            default=str,
        )
        lines.append(f"expected_state：{payload[:4000]}")
    if contract.inputs:
        payload = json.dumps(contract.inputs, ensure_ascii=False, default=str)
        lines.append(f"本次调用 inputs：{payload[:4000]}")
    if contract.interaction_intent is not None:
        payload = json.dumps(
            contract.interaction_intent.model_dump(mode="json"),
            ensure_ascii=False,
            default=str,
        )
        lines.append(f"interaction_intent：{payload[:4000]}")
    if contract.persistence:
        lines.append(f"persistence：{contract.persistence}")
    if contract.required_values:
        rendered = ", ".join(
            f"{field}={','.join(target_value_options(value))}"
            for field, value in contract.required_values.items()
        )
        lines.append(f"required_values：{rendered}")
    return lines


def _contract_requirements(contract: StatementContract) -> list[str]:
    requirements: list[str] = []
    intent = contract.interaction_intent
    if intent is not None:
        proof = (
            (
                f"唯一集合 entity={intent.entity!r} 覆盖 fields={intent.required_fields!r}，"
                f"且 expected_state={contract.expected_state!r} 的每一项均有证据"
            )
            if intent.phase != "constrain"
            else f"完整 applied_filter_state 精确等于 {intent.predicates!r}"
        )
        requirements.append(
            f"这是 {intent.phase}_collection："
            f"只有结构证据证明 {proof} 时才可 complete。"
        )
    if contract.persistence == "explicit_commit":
        requirements.append(
            "合同要求 explicit_commit 持久化边界；是否已越过只由 Journal receipt 证明。"
        )
    return requirements


def _durable_from_turn(turn: PolicyTurn, statement_id: str) -> list[DurableFact]:
    facts: list[DurableFact] = []
    ref = _event_ref(turn)
    signal = turn.action_signal
    role = _role(turn)
    instr = _instruction(turn)

    if signal is not None:
        signal_role = str(signal.role or role or "")
        if signal.execution in {"dispatch_failed", "not_attempted"}:
            detail = "; ".join(signal.evidence) or signal.suppressed_reason
            facts.append(DurableFact(
                kind=(
                    "dispatch_failure"
                    if signal.execution == "dispatch_failed"
                    else "grounding_failure"
                ),
                text=(
                    f"动作未执行：{instr or signal.action_key or signal_role}"
                    + (f"；{detail}" if detail else "")
                ),
                event_ref=ref,
            ))
        if signal.execution == "dispatched":
            response = (
                f" response={signal.response}"
                f" channels={','.join(signal.response_channels)}"
                if signal.response != "unknown" or signal.response_channels
                else ""
            )
            facts.append(DurableFact(
                kind="action_receipt",
                text=(
                    f"已派发 {signal_role or 'action'}："
                    f"{instr or signal.target_control or signal.action_key}{response}"
                ),
                event_ref=ref,
                metadata={
                    "role": signal_role,
                    "action_key": signal.action_key,
                    "surface_id": signal.surface_id,
                    "target_control": signal.target_control,
                    "response": signal.response,
                    "expected_result": _expected_result(turn),
                },
            ))
            if signal.mutation_receipt is not None:
                receipt = signal.mutation_receipt
                facts.append(DurableFact(
                    kind="mutation_receipt",
                    text=(
                        f"mutation receipt role={signal_role} "
                        f"field={receipt.field} value={receipt.intended_value!r} "
                        f"subject={receipt.subject_ref}"
                    ),
                    event_ref=ref,
                    metadata={
                        "role": signal_role,
                        "field": receipt.field,
                        "value": receipt.intended_value,
                        "subject_ref": receipt.subject_ref,
                    },
                ))
        if signal.target == "off_target":
            actual = ""
            if turn.target_verify is not None:
                actual = turn.target_verify.actual_element or turn.target_verify.reason
            elif signal.binding is not None and signal.binding.status == "contradicted":
                actual = signal.binding.reason
            facts.append(DurableFact(
                kind="off_target",
                text=f"落点偏离目标：{instr or ''}{(' → ' + actual) if actual else ''}",
                event_ref=ref,
            ))
        if turn.target_verify is not None and not turn.target_verify.on_target:
            facts.append(DurableFact(
                kind="off_target",
                text=(
                    f"TargetVerify off_target：{turn.target_verify.actual_element or ''} "
                    f"{turn.target_verify.reason or ''}"
                ).strip(),
                event_ref=ref,
            ))

    # An externally-read value (e.g. an SMS verification code the agent read while
    # visiting Messages) is a perception-layer fact extracted from the observation
    # semantic tree at turn-recording time (turn.read_code), not an LLM belief.
    # Without it the value only lives in the transition narrative and evaporates once
    # the agent leaves the external app — so the fill step after returning has nothing
    # to anchor on.
    code = _read_code(turn)
    if code:
        facts.append(DurableFact(
            kind="external_read",
            text=f"已读取验证码 {code}，待填入目标表单",
            event_ref=ref,
            metadata={"code": code},
        ))

    return facts


def _step_summary(turn: PolicyTurn) -> str:
    parts: list[str] = []
    if turn.operation_mode != "interactive":
        parts.append(f"mode={turn.operation_mode}")
    instr = _instruction(turn)
    role = _role(turn)
    if instr:
        parts.append(f"指令[{role or '?'}]：{instr}")
    if turn.executed and turn.action_decision and turn.action_decision.action:
        a = turn.action_decision.action
        parts.append(f"执行：{a.action_type} {a.description}")
    elif turn.supervisor and turn.supervisor.action_intent is None:
        parts.append("无派发动作（观察/裁决）")
    signal = turn.action_signal
    if signal is not None:
        parts.append(
            f"signal exec={signal.execution} target={signal.target} response={signal.response}"
        )
    if turn.transition is not None:
        validation_error = str(turn.transition.get("validation_error") or "")
        if validation_error:
            parts.append(f"机械校验失败：{validation_error}")
    if turn.no_effect:
        parts.append("no_effect")
    return "；".join(p for p in parts if p) or "(empty turn)"


def turns_for_instance(
    history: list[PolicyTurn],
    *,
    instance_id: str,
    statement_id: str = "",
) -> list[PolicyTurn]:
    """Filter journal turns belonging to this statement invocation."""
    if instance_id:
        return [
            turn for turn in history
            if turn.statement_instance_id == instance_id
        ]
    if statement_id:
        return [
            turn for turn in history
            if turn.supervisor is not None
            and turn.supervisor.statement_id == statement_id
        ]
    return list(history)


def build_memory_view(
    *,
    instance_id: str,
    contract: StatementContract,
    history: list[PolicyTurn],
    observation: Observation | None = None,
    previous_statement: dict[str, str] | None = None,
    recent_k: int = DEFAULT_RECENT_K,
    compressed_k: int = DEFAULT_COMPRESSED_K,
) -> StatementMemoryView:
    """Project journal turns into a frozen StatementMemoryView.

    ``observation`` is accepted for API stability (current frame is passed separately to
    the LLM); it is not written into durable facts (those come only from journal).
    """
    del observation  # current frame is not a historical fact
    scoped = turns_for_instance(
        history,
        instance_id=instance_id,
        statement_id=contract.id,
    )
    durable: list[DurableFact] = []
    seen_keys: dict[tuple[str, ...], int] = {}
    for turn in scoped:
        for fact in _durable_from_turn(turn, contract.id):
            key = (fact.kind, fact.event_ref, fact.text)
            previous = seen_keys.get(key)
            if previous is None:
                seen_keys[key] = len(durable)
                durable.append(fact)
            else:
                durable[previous] = fact

    k = max(0, int(recent_k))
    if k == 0 or len(scoped) <= k:
        older, recent = [], scoped
    else:
        older, recent = scoped[:-k], scoped[-k:]

    compressed_rows = [
        f"[{_event_ref(turn)}] {_step_summary(turn)}"
        for turn in older
    ]
    compressed_limit = max(0, int(compressed_k))
    compressed = (
        compressed_rows[-compressed_limit:]
        if compressed_limit
        else []
    )
    recent_steps = tuple(
        RecentStep(event_ref=_event_ref(turn), text=_step_summary(turn))
        for turn in recent
    )

    # The in-flight sub-goal: the most recent dispatched action's expected result
    # plus the most recent declared open gap (pending fill/return step).
    pending_result = ""
    pending_gap = ""
    for turn in reversed(scoped):
        if pending_result and pending_gap:
            break
        if not pending_result:
            expected = _expected_result(turn)
            if expected:
                pending_result = expected
        if not pending_gap:
            gap = _pending_gap(turn)
            if gap:
                pending_gap = gap

    # Advance a stale "read" gap. The assessment re-frames the empty code field as
    # "尚未读取验证码" each time the agent returns to the form, so the pending gap
    # never converges to the fill step on its own. When an external_read fact already
    # holds the code, the read step is done — the pending gap becomes the fill step.
    read_code = ""
    for fact in durable:
        if fact.kind == "external_read" and fact.metadata.get("code"):
            read_code = str(fact.metadata["code"])
    if (
        read_code
        and pending_gap
        and "读取" in pending_gap
        and "填入" not in pending_gap
        and read_code not in pending_gap
    ):
        pending_gap = f"将已读取的验证码 {read_code} 填入登录表单并提交"

    return StatementMemoryView(
        instance_id=instance_id,
        statement_id=contract.id,
        contract_lines=tuple(_contract_lines(contract)),
        contract_requirements=tuple(_contract_requirements(contract)),
        durable_facts=tuple(durable),
        recent_steps=recent_steps,
        compressed_history=tuple(compressed),
        previous_statement=previous_statement,
        pending_result=pending_result,
        pending_gap=pending_gap,
    )


def durable_kinds_present(view: StatementMemoryView) -> set[str]:
    """Test helper: set of durable fact kinds retained after build."""
    return {f.kind for f in view.durable_facts}


def available_event_refs(view: StatementMemoryView) -> set[str]:
    """Fact-bearing Journal references that may support a terminal proposal.

    Narrative summaries can include prior model beliefs, so merely appearing in the recent
    window does not make a turn evidentiary. Cross-frame completion may cite only turns that
    produced a durable receipt/effect/failure fact; current-frame evidence uses its dedicated
    ``current_observation`` source.
    """
    return {fact.event_ref for fact in view.durable_facts if fact.event_ref}


__all__ = [
    "DEFAULT_RECENT_K",
    "DEFAULT_COMPRESSED_K",
    "DurableFact",
    "RecentStep",
    "StatementMemoryView",
    "build_memory_view",
    "available_event_refs",
    "durable_kinds_present",
    "turns_for_instance",
]
