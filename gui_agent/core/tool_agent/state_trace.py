"""Pure text editing for continuous Worker State memory."""

from __future__ import annotations

from typing import Any

from gui_agent.core.tool_agent.contracts import (
    WorkerSpec,
    WorkerStateEditBatch,
    WorkerStateSnapshot,
    WorkerStateTarget,
)
from gui_agent.core.tool_agent.worker_memory.journal import WorkerJournal


def initial_worker_state(spec: WorkerSpec) -> WorkerStateSnapshot:
    del spec
    return WorkerStateSnapshot(
        summary="No goal-relevant facts have been observed yet.",
    )


def latest_runtime_receipt(journal: WorkerJournal) -> dict[str, Any] | None:
    """Project the latest receipt as observation context, never as State."""

    event = next((item for item in reversed(journal.events) if item.receipt), None)
    if event is None or event.receipt is None:
        return None
    receipt = event.receipt
    outcome: dict[str, Any] = {
        "kind": receipt.outcome.kind,
        "action_type": receipt.outcome.action_type or None,
    }
    if receipt.outcome.target:
        outcome["target"] = receipt.outcome.target
    projected: dict[str, Any] = {
        "receipt_ref": event.event_ref,
        "tool": receipt.tool,
        "executed": receipt.executed,
        "outcome": outcome,
    }
    if receipt.target_ref:
        projected["target_ref"] = receipt.target_ref
    if receipt.state_target_ref:
        projected["state_target_ref"] = receipt.state_target_ref
    if description := str(receipt.args.get("description") or "").strip():
        projected["action_description"] = description
    if receipt.tool in {"scroll", "drag"} and receipt.args.get("direction"):
        projected["traversal_direction"] = receipt.args["direction"]
    return projected


def _apply_markdown_edits(memory: str, batch: WorkerStateEditBatch) -> str:
    """Apply exact edits without interpreting the Markdown body."""

    updated = memory
    for index, edit in enumerate(batch.edits, start=1):
        old_text = "\n".join(edit.old_lines)
        new_text = "\n".join(edit.new_lines)
        if old_text == new_text:
            raise ValueError(f"State edit {index} does not change memory")
        if not old_text:
            # Empty old_lines is an append: a fresh memory sets the body, a non-empty
            # memory gains the new observation at the end. The historical hard error
            # ("empty old_lines for non-empty memory") aborted runs whenever the State
            # appended rather than anchored-replaced; appending is the recoverable intent.
            updated = (updated.rstrip() + "\n\n" + new_text) if updated else new_text
        else:
            occurrences = updated.count(old_text)
            if occurrences != 1:
                # The anchor block is missing (0, memory drifted / model mis-reproduced)
                # or ambiguous (>1, the text appears more than once). In both cases the
                # State's new observation is preserved by appending it instead of
                # aborting the run; the exact-match replace applies only when the anchor
                # occurs exactly once.
                updated = updated.rstrip() + "\n\n" + new_text
            else:
                updated = updated.replace(old_text, new_text, 1)
        if len(updated) > 48_000:
            raise ValueError("State Markdown memory exceeds 48000 characters")
    return updated.strip()


def _summary(state: WorkerStateSnapshot) -> str:
    visible = sum(
        target.visibility != "not_visible" for target in state.targets.values()
    )
    return (
        f"Surface={state.surface or 'not observed'}; "
        f"tracked targets={len(state.targets)}; visible targets={visible}; "
        f"memory chars={len(state.markdown)}."
    )


def reduce_worker_state(
    previous: WorkerStateSnapshot | None,
    batch: WorkerStateEditBatch,
    *,
    spec: WorkerSpec,
) -> WorkerStateSnapshot:
    """Apply one generic Markdown edit and refresh current target bindings."""

    expected_mode = "init" if previous is None else "edit"
    if batch.mode != expected_mode:
        raise ValueError(f"expected State mode {expected_mode!r}, got {batch.mode!r}")
    state = initial_worker_state(spec) if previous is None else previous.model_copy(deep=True)
    state.frame_id = batch.frame_id
    if batch.surface is not None:
        state.surface = batch.surface
    state.markdown = _apply_markdown_edits(state.markdown, batch)

    for target in state.targets.values():
        target.visibility = "not_visible"
        target.owned_region_visibility = "not_visible"
    seen: set[str] = set()
    for item in batch.visible_targets:
        if item.target_ref in seen:
            raise ValueError(f"duplicate visible target ref {item.target_ref!r}")
        seen.add(item.target_ref)
        target = state.targets.get(item.target_ref)
        if target is None:
            target = WorkerStateTarget(identity=item.identity)
            state.targets[item.target_ref] = target
        target.identity = item.identity
        target.visibility = item.visibility
        target.owned_region_visibility = item.owned_region_visibility

    # Keep the current-frame bindings in State's observed order. Existing dict
    # insertion order reflects first discovery and can misalign a textual binding
    # with the current screenshot after navigation or list changes.
    state.targets = {
        ref: state.targets[ref]
        for ref in (
            *(item.target_ref for item in batch.visible_targets),
            *(ref for ref in state.targets if ref not in seen),
        )
    }

    state.summary = _summary(state)
    return state


def state_continuation_payload(state: WorkerStateSnapshot) -> dict[str, Any]:
    """Return the exact document and stable binding registry for the next edit."""

    return {
        "surface": state.surface,
        "target_registry": {
            ref: target.identity for ref, target in state.targets.items()
        },
        "memory_markdown": state.markdown,
    }


def _markdown_section_body(markdown: str, target_ref: str) -> list[str]:
    """Copy the heading body for a target_ref without interpreting the facts."""

    heading = f"### {target_ref}"
    collecting = False
    body: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("### "):
            if collecting:
                break
            collecting = line.strip() == heading
            continue
        if collecting:
            body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return [line for line in body if line.strip()]


def state_actor_markdown(state: WorkerStateSnapshot) -> str:
    """Render the model-owned document beside Runtime-owned current bindings."""

    visible = [
        (ref, target)
        for ref, target in state.targets.items()
        if target.visibility != "not_visible"
    ]
    lines = [
        f"Surface: `{state.surface or 'not observed'}`",
        "",
        "## Currently visible targets",
        "",
        (
            "Each backticked ID is the exact `state_target_ref` to copy when acting "
            "on that visible target. Order has no spatial or priority meaning. Facts "
            "omitted below are unobserved, not absent."
        ),
    ]
    if not visible:
        lines.append("- None")
    else:
        for ref, target in visible:
            lines.append(
                f"- `{ref}` — {target.identity} "
                f"({target.visibility}, {target.owned_region_visibility})"
            )
            lines.extend(
                f"  {fact}"
                for fact in _markdown_section_body(state.markdown, ref)
            )
    lines.extend([
        "",
        "## Continuous target-oriented memory",
        "",
        state.markdown or "(No durable facts recorded yet.)",
    ])
    return "\n".join(lines)


def state_observation_focus(spec: WorkerSpec) -> dict[str, Any]:
    """Expose fact shapes and the goal contract to State.

    State owns the goal-establishment judgment, so it receives the success criteria
    and the declared completion facts (with expected values). It still does not
    recommend actions — that remains the Actor's decision.
    """

    visible_fields = sorted({
        str(value)
        for requirement in spec.data_requirements
        for value in (
            *requirement.field_sources.values(),
            *(requirement.row_schema.get("properties") or {}).keys(),
        )
        if str(value).strip()
    })
    return {
        "visible_fields": visible_fields,
        "goal_contract": {
            "success_criteria": list(spec.success_criteria),
            "completion_facts": [
                {
                    "property_ref": item.property_ref,
                    "description": item.description,
                    "expected_value": item.expected_value,
                }
                for item in spec.completion_facts
            ],
        },
    }
