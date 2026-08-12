"""Stable local service boundary for Tool Agent clients."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

from gui_agent.core.runtime.io import (
    capture_stdio,
    create_run_dir,
    get_log_root,
    tee_stdio,
)
from gui_agent.core.runtime.factory import SetupCheckResult, build_platform
from gui_agent.core.self_learning.app_summary import (
    auto_discover_knowledge,
    load_knowledge_for_app,
    match_app_by_url,
)
from gui_agent.core.tool_agent.result import execute_tool_agent


PlatformName = Literal["browser", "android"]
PerceptionMode = Literal["vision-only", "enhanced"]


@dataclass(frozen=True)
class ToolAgentServiceResult:
    """Serializable summary returned by CLI and MCP clients."""

    run_id: str
    run_dir: str
    platform: str
    phase: str
    task_type: str
    summary: str
    output: Any
    reply: str
    context_path: str
    trace_path: str
    replay_path: str
    report_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode_output(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _fallback_task_type(goal: str) -> str:
    text = goal.strip().lower()
    mutation_markers = (
        "create", "add", "edit", "update", "change", "delete", "remove",
        "set ", "submit", "place", "enable", "disable", "assign", "save",
        "mark", "rename", "notify", "send", "turn on", "turn off",
    )
    navigation_markers = ("open ", "go to", "navigate", "visit")
    if any(marker in text for marker in mutation_markers):
        return "MUTATE"
    if any(marker in text for marker in navigation_markers):
        return "NAVIGATE"
    return "RETRIEVE"


class ToolAgentService:
    """Own platform setup, session lifecycle, logging and result projection."""

    def __init__(self, *, log_root: Path | None = None) -> None:
        self.log_root = (log_root or get_log_root()).expanduser().resolve()

    def check_environment(
        self,
        platform: PlatformName,
        **platform_options: object,
    ) -> SetupCheckResult:
        return build_platform(platform, **platform_options).setup_check()

    def run(
        self,
        goal: str,
        *,
        platform: PlatformName,
        perception_mode: PerceptionMode = "enhanced",
        max_turns: int = 50,
        allow_multi_action: bool = True,
        show_hud: bool = True,
        mirror_stdio: bool = False,
        knowledge: str | None = None,
        access_context: str | None = None,
        stop_requested: Callable[[], bool] | None = None,
        on_run_created: Callable[[str, Path], None] | None = None,
        **platform_options: object,
    ) -> ToolAgentServiceResult:
        if not goal.strip():
            raise ValueError("goal must not be empty")
        if not 1 <= max_turns <= 50:
            raise ValueError("max_turns must be between 1 and 50")

        bundle = build_platform(platform, **platform_options)
        setup = bundle.setup_check()
        if not setup.ok:
            detail = "\n".join(setup.lines)
            raise RuntimeError(f"{setup.summary}\n{detail}".strip())

        log_dir = create_run_dir(
            "tool_agent",
            platform,
            log_root=self.log_root,
        )
        run_id = str(log_dir.resolve().relative_to(self.log_root))
        if on_run_created is not None:
            on_run_created(run_id, log_dir.resolve())

        stdio = tee_stdio(log_dir) if mirror_stdio else capture_stdio(log_dir)
        with stdio:
            for line in setup.lines:
                print(line)
            print(f"Platform: {platform}")
            print(f"Log Dir : {log_dir}")

            hud = bundle.make_status_reporter(show_hud)
            try:
                with bundle.open_session() as session:
                    page_url = ""
                    page_title = ""
                    site_name = ""
                    device = getattr(session, "client", None)
                    if device is not None and hasattr(device, "page_info"):
                        try:
                            page_url, page_title = device.page_info()
                            site_name = match_app_by_url(page_url, platform) or ""
                        except Exception as exc:  # noqa: BLE001
                            print(f"Initial page probe unavailable: {exc}")

                    app_knowledge = auto_discover_knowledge(goal, platform)
                    if app_knowledge is None and site_name:
                        app_knowledge = load_knowledge_for_app(site_name, platform)
                    effective_knowledge = knowledge
                    effective_access = access_context
                    knowledge_summary = None
                    if app_knowledge is not None:
                        if effective_knowledge is None:
                            effective_knowledge = app_knowledge.orchestrator_context(goal)
                        if effective_access is None:
                            effective_access = app_knowledge.deployment
                        knowledge_summary = app_knowledge.summary()

                    result, presentation = execute_tool_agent(
                        intent=goal,
                        bundle=bundle,
                        session=session,
                        log_dir=log_dir,
                        perception_mode=perception_mode,
                        max_turns=max_turns,
                        allow_multi_action=allow_multi_action,
                        fallback_task_type=_fallback_task_type(goal),
                        knowledge_summary=knowledge_summary,
                        knowledge=effective_knowledge or "",
                        access_context=effective_access or "",
                        page_url=page_url,
                        page_title=page_title,
                        hud=hud,
                        stop_requested=stop_requested,
                    )
            finally:
                if hud is not None and hasattr(hud, "close"):
                    hud.close()

            report_path: Path | None = None
            context_path = log_dir / "context.json"
            if context_path.exists():
                try:
                    from gui_agent.reports import RunnerReportBuilder, save_report

                    report_path = save_report(
                        RunnerReportBuilder().build(log_dir),
                        log_dir / "report.html",
                    )
                    print(f"Report  : {report_path}")
                except Exception as exc:  # noqa: BLE001
                    print(f"Report generation unavailable: {exc}")

        return ToolAgentServiceResult(
            run_id=run_id,
            run_dir=str(log_dir.resolve()),
            platform=platform,
            phase=result.phase,
            task_type=result.task_type or _fallback_task_type(goal),
            summary=result.summary,
            output=_decode_output(result.output),
            reply=presentation.reply,
            context_path=str((log_dir / "context.json").resolve()),
            trace_path=str((log_dir / "tool_agent_trace.json").resolve()),
            replay_path=str((log_dir / "tool_agent_replay.json").resolve()),
            report_path=str(report_path.resolve()) if report_path else None,
        )

    def _resolve_run_dir(self, run_id: str) -> Path:
        run_dir = (self.log_root / run_id).resolve()
        if self.log_root not in run_dir.parents:
            raise ValueError("run_id resolves outside GUIWeave's log root")
        relative = run_dir.relative_to(self.log_root)
        if (
            len(relative.parts) != 3
            or relative.parts[0] != "tool_agent"
            or relative.parts[1] not in {"browser", "android"}
        ):
            raise ValueError("run_id is not a Tool Agent run")
        return run_dir

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _read_events(self, run_dir: Path) -> list[dict[str, Any]]:
        """Read the live JSONL stream, falling back to the final trace artifact."""

        live_path = run_dir / "tool_agent_events.jsonl"
        live_events: list[dict[str, Any]] = []
        if live_path.is_file():
            try:
                for line in live_path.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        live_events.append(event)
            except OSError:
                pass
        if live_events:
            return live_events
        trace = self._read_json(run_dir / "tool_agent_trace.json")
        events = trace.get("trace")
        return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []

    def _project_run(self, run_dir: Path) -> dict[str, Any]:
        context_path = run_dir / "context.json"
        trace_path = run_dir / "tool_agent_trace.json"
        context = self._read_json(context_path)
        trace = self._read_json(trace_path)
        events = self._read_events(run_dir)
        outcome = context.get("outcome") if isinstance(context.get("outcome"), dict) else {}
        phase = str(outcome.get("phase") or trace.get("phase") or "starting")
        if events and phase == "starting":
            phase = "running"
        if any(
            isinstance(event, dict) and event.get("event") == "runtime_interrupted"
            for event in events
        ):
            phase = "interrupted"
        summary = str(outcome.get("summary") or trace.get("summary") or "")
        try:
            run_id = str(run_dir.relative_to(self.log_root))
        except ValueError as exc:  # pragma: no cover - internal invariant
            raise ValueError("run directory resolves outside GUIWeave's log root") from exc
        platform = str(context.get("platform") or run_dir.parent.name)
        live_goal = next(
            (
                str(event.get("goal") or "")
                for event in events
                if isinstance(event, dict) and event.get("event") == "runtime_started"
            ),
            "",
        )
        modified_at = datetime.fromtimestamp(
            run_dir.stat().st_mtime
        ).astimezone().isoformat(timespec="seconds")
        artifacts = {
            name: str(path)
            for name, filename in (
                ("report", "report.html"),
                ("trace", "tool_agent_trace.json"),
                ("replay", "tool_agent_replay.json"),
                ("stdout", "stdout.log"),
                ("stderr", "stderr.log"),
            )
            if (path := run_dir / filename).is_file()
        }
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "goal": context.get("goal") or live_goal,
            "platform": platform,
            "phase": phase,
            "summary": summary,
            "outcome": outcome or None,
            "reply": context.get("reply"),
            "orchestrator": context.get("orchestrator"),
            "models": context.get("models") or {},
            "modified_at": modified_at,
            "event_count": len(events),
            "artifacts": artifacts,
            "report_path": artifacts.get("report"),
        }

    def list_runs(
        self,
        *,
        platform: PlatformName | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List Tool Agent runs newest-first from durable local artifacts."""

        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        platforms = (platform,) if platform else ("browser", "android")
        candidates: list[Path] = []
        for platform_name in platforms:
            base = self.log_root / "tool_agent" / platform_name
            if not base.is_dir():
                continue
            candidates.extend(path for path in base.iterdir() if path.is_dir())
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [self._project_run(path) for path in candidates[:limit]]

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Read a prior or active run without allowing paths outside the log root."""

        run_dir = self._resolve_run_dir(run_id)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"unknown run_id: {run_id}")
        return self._project_run(run_dir)

    def get_run_events(self, run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Read a bounded tail of structured runtime events."""

        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        run_dir = self._resolve_run_dir(run_id)
        events = self._read_events(run_dir)
        return events[-limit:]

    def get_artifact_path(self, run_id: str, artifact: str) -> Path:
        """Resolve one explicitly supported run artifact."""

        names = {
            "report": "report.html",
            "trace": "tool_agent_trace.json",
            "replay": "tool_agent_replay.json",
            "stdout": "stdout.log",
            "stderr": "stderr.log",
        }
        if artifact not in names:
            raise ValueError(f"unsupported artifact: {artifact}")
        path = self._resolve_run_dir(run_id) / names[artifact]
        if not path.is_file():
            raise FileNotFoundError(f"artifact not found: {artifact}")
        return path


__all__ = ["ToolAgentService", "ToolAgentServiceResult"]
