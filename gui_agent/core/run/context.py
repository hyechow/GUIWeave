"""Context persistence helpers used by runner entrypoints."""

from __future__ import annotations

import json
from pathlib import Path

from gui_agent.core.schemas import Observation, PolicyContext


OBSERVATION_SNAPSHOT_VERSION = 1


def write_json_atomic(path: Path, payload: object) -> None:
    """Write one JSON checkpoint without exposing a partially-written target file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def save_context(path: Path, context: PolicyContext) -> None:
    write_json_atomic(path, context.model_dump(mode="json"))


def save_observation_snapshot(
    path: Path,
    observation: Observation,
    *,
    screenshot: str,
) -> None:
    """Persist the structured half of one observed frame for deterministic replay.

    PNG bytes already live in the adjacent screenshot. Keeping them out of JSON avoids a second
    base64 copy while preserving every adapter-provided signal consumed by the supervisor.
    """
    payload = {
        "version": OBSERVATION_SNAPSHOT_VERSION,
        "screenshot": screenshot,
        "observation": observation.model_dump(mode="json", exclude={"png_bytes"}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_observation_snapshot(path: Path) -> Observation:
    """Load a replayable observation and its adjacent screenshot."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != OBSERVATION_SNAPSHOT_VERSION:
        raise ValueError(
            f"unsupported observation snapshot version {version!r}; "
            f"expected {OBSERVATION_SNAPSHOT_VERSION}"
        )
    screenshot = path.parent / str(payload["screenshot"])
    if not screenshot.is_file():
        raise FileNotFoundError(f"replay screenshot not found: {screenshot}")
    return Observation.model_validate(
        {**payload["observation"], "png_bytes": screenshot.read_bytes()}
    )


def extract_action_plan(supervisor: object) -> dict | None:
    action_plan = getattr(supervisor, "_last_action_plan", None)
    if action_plan is None:
        return None
    return action_plan.model_dump(exclude_none=True)


def extract_transition(supervisor: object) -> dict | None:
    """Return the final validated Transition record for this turn, if any."""
    record = getattr(supervisor, "_last_transition_record", None)
    return dict(record) if isinstance(record, dict) else None


def load_context(
    path: Path,
    prompt: str,
    supervisor_name: str,
    action_name: str,
    raw_input: str | None = None,
    router: dict | None = None,
) -> PolicyContext:
    if path.exists():
        return PolicyContext.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return PolicyContext(
        goal=prompt,
        supervisor_policy_name=supervisor_name,
        action_policy_name=action_name,
        raw_input=raw_input if raw_input is not None else prompt,
        router=router,
    )
