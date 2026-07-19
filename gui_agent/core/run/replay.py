"""Pure offline checkpoint replay for one GUIWeave run directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gui_agent.core.orchestrator.program import Program, StatementNode
from gui_agent.core.run.collection_view import build_collection_view, coverage_status
from gui_agent.core.run.context import load_observation_snapshot
from gui_agent.core.run.program_runtime import ProgramRuntime
from gui_agent.core.schemas import PolicyContext, StatementContract


class ReplayError(RuntimeError):
    """The checkpoint cannot be deterministically reconstructed."""


def _context_path(source: Path) -> Path:
    path = source.expanduser().resolve()
    if path.is_dir():
        path = path / "context.json"
    if not path.is_file():
        raise ReplayError(f"context checkpoint not found: {path}")
    return path


def _fallback_program(context: PolicyContext) -> Program:
    revisions = context.journal.program_revisions
    payload = revisions[0].program if revisions else None
    if payload is None and isinstance(context.orchestrator, dict):
        payload = context.orchestrator.get("program")
    if not payload:
        raise ReplayError("checkpoint has no Program revision")
    return Program.model_validate(payload)


def _runtime_summary(runtime: ProgramRuntime) -> dict[str, Any]:
    return {
        "finished": runtime.finished,
        "index": runtime.index,
        "current": (
            {
                "id": runtime.current.id,
                "executor": runtime.current.executor,
                "instance_id": runtime.current_instance_id,
            }
            if runtime.current is not None
            else None
        ),
        "env": runtime.interpreter.env,
        "run_log": [
            {
                "node_id": record.node_id,
                "executor": record.executor,
                "phase": record.result.phase,
                "verification": record.result.verification,
                "instance_id": record.instance_id,
            }
            for record in runtime.interpreter.run_log
        ],
        "reply": runtime.reply,
        "recovery": runtime.recovery_summary() if runtime.has_recovery else None,
    }


def _statement_nodes(program: Program) -> dict[str, StatementNode]:
    nodes: dict[str, StatementNode] = {}

    def visit(statements: list[Any]) -> None:
        for statement in statements:
            if isinstance(statement, StatementNode):
                nodes[statement.id] = statement
            elif statement.op == "if":
                visit(statement.then)
                visit(statement.otherwise)
            elif statement.op == "foreach":
                visit(statement.body)

    visit(program.statements)
    return nodes


def _contract_for_instance(
    context: PolicyContext,
    program: Program,
    instance_id: str,
    statement_id: str,
) -> StatementContract | None:
    info = next(
        (
            turn.statement
            for turn in reversed(context.journal.turns)
            if turn.statement_instance_id == instance_id and turn.statement is not None
        ),
        None,
    )
    if info is not None:
        return StatementContract(
            id=statement_id,
            goal=info.goal,
            success=info.success,
            inputs=dict(info.inputs),
            required_values=dict(info.required_values),
            returns=dict(info.returns),
            persistence=info.persistence,
        )
    node = _statement_nodes(program).get(statement_id)
    if node is not None and any(
        spec.type == "list[record]" for spec in node.returns.values()
    ):
        return StatementContract(
            id=statement_id,
            goal=node.goal_text,
            success=str(getattr(node, "success", node.goal_text)),
            required_values=dict(getattr(node, "required_values", {})),
            returns=dict(node.returns),
            persistence=getattr(node, "persistence", "immediate"),
        )
    return None


def _collection_summaries(
    context: PolicyContext,
    program: Program,
) -> tuple[list[dict[str, Any]], list[str]]:
    by_instance: dict[str, str] = {}
    receipts = list(getattr(context.journal, "acquisition_receipts", []))
    for event in [*context.journal.collection_slices, *receipts]:
        by_instance[event.statement_instance_id] = event.statement_id
    summaries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for instance_id, statement_id in sorted(by_instance.items()):
        contract = _contract_for_instance(context, program, instance_id, statement_id)
        if contract is None:
            warnings.append(
                f"cannot reconstruct collection contract for {instance_id} ({statement_id})"
            )
            continue
        view = build_collection_view(
            instance_id=instance_id,
            contract=contract,
            history=context.journal.events,
        )
        instance_receipts = [
            receipt for receipt in receipts
            if receipt.statement_instance_id == instance_id
            and receipt.statement_id == statement_id
        ]
        if instance_receipts:
            # Acquire is an optional executor from this command's perspective. Lazy
            # import keeps the replay-only commit runnable before that IR is present.
            from gui_agent.core.run.statements.acquire import build_acquire_memory

            memory = build_acquire_memory(
                context.journal,
                instance_id=instance_id,
                statement_id=statement_id,
            )
            bound_region = memory.bound_region
            failed_capabilities = sorted(memory.failed_capabilities)
        else:
            bound_region = ""
            failed_capabilities = []
        summaries.append({
            "instance_id": instance_id,
            "statement_id": statement_id,
            "collection_key": view.collection_key,
            "records": len(view.records),
            "segments": len(view.observed_segments),
            "coverage": coverage_status(view),
            "may_contain_duplicates": view.may_contain_duplicates,
            "provenance_drift": view.provenance_drift,
            "bound_region": bound_region,
            "attempts": sum(
                receipt.action_family != "bind_region" for receipt in instance_receipts
            ),
            "failed_capabilities": failed_capabilities,
        })
    return summaries, warnings


def _observation_summaries(log_dir: Path) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for path in sorted(
        log_dir.glob("observation_turn_*.json"),
        key=lambda item: int(item.stem.rsplit("_", 1)[-1]),
    ):
        observation = load_observation_snapshot(path)
        frames.append({
            "snapshot": path.name,
            "source": observation.source,
            "url": observation.url,
            "tables": len(observation.tables or []),
            "form_controls": len(observation.form_controls or []),
        })
    return frames


def replay_log(source: Path | str) -> dict[str, Any]:
    """Rebuild all deterministic state without opening a platform or invoking an LLM."""
    context_path = _context_path(Path(source))
    context = PolicyContext.model_validate_json(context_path.read_text(encoding="utf-8"))
    program = _fallback_program(context)
    runtime = ProgramRuntime.resume(program, context.journal)
    runtime_summary = _runtime_summary(runtime)
    collections, warnings = _collection_summaries(context, program)

    # A JSON round-trip is the checkpoint boundary. Replaying it must produce the
    # same logical runtime and collection projections as the live in-memory model.
    restored = PolicyContext.model_validate(context.model_dump(mode="json"))
    restored_program = _fallback_program(restored)
    if _runtime_summary(
        ProgramRuntime.resume(restored_program, restored.journal)
    ) != runtime_summary:
        raise ReplayError("ProgramRuntime differs after checkpoint round-trip")
    restored_collections, restored_warnings = _collection_summaries(
        restored, restored_program,
    )
    if restored_collections != collections:
        raise ReplayError("collection projections differ after checkpoint round-trip")
    warnings.extend(restored_warnings)

    frames = _observation_summaries(context_path.parent)
    snapshot_names = {frame["snapshot"] for frame in frames}
    missing_snapshots = [
        f"observation_turn_{turn.index}.json"
        for turn in context.journal.turns
        if turn.operation_mode != "non_interactive"
        and f"observation_turn_{turn.index}.json" not in snapshot_names
    ]
    if missing_snapshots:
        warnings.append(
            "missing interactive observation snapshots: "
            + ", ".join(missing_snapshots)
        )
    return {
        "version": 1,
        "mode": "state",
        "offline": True,
        "valid": True,
        "log_dir": str(context_path.parent),
        "context": context_path.name,
        "journal": {
            "events": len(context.journal.events),
            "turns": len(context.journal.turns),
            "statement_outcomes": len(context.journal.statement_outcomes),
            "program_revisions": len(context.journal.program_revisions),
            "collection_slices": len(context.journal.collection_slices),
            "acquisition_receipts": len(
                getattr(context.journal, "acquisition_receipts", [])
            ),
        },
        "observations": {"snapshots": len(frames), "frames": frames},
        "runtime": runtime_summary,
        "collections": collections,
        "warnings": sorted(set(warnings)),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    journal = summary["journal"]
    runtime = summary["runtime"]
    print(f"[replay] OK {summary['log_dir']}")
    print(
        "  journal  "
        f"events={journal['events']} turns={journal['turns']} "
        f"outcomes={journal['statement_outcomes']} revisions={journal['program_revisions']}"
    )
    print(
        "  runtime  "
        f"finished={runtime['finished']} index={runtime['index']} "
        f"run_log={len(runtime['run_log'])} env={sorted(runtime['env'])}"
    )
    print(f"  observations  snapshots={summary['observations']['snapshots']}")
    for collection in summary["collections"]:
        print(
            f"  collection  {collection['instance_id']} records={collection['records']} "
            f"coverage={collection['coverage']} attempts={collection['attempts']}"
        )
    for warning in summary["warnings"]:
        print(f"  warning  {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline replay of a GUIWeave run checkpoint (never invokes an LLM)."
    )
    parser.add_argument("source", type=Path, help="run directory or context.json")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        summary = replay_log(args.source)
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports validation failures
        if args.json:
            print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[replay] FAIL {exc}")
        return 1
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ReplayError", "main", "replay_log"]
