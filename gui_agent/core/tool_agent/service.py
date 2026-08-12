"""Stable local service boundary for Tool Agent clients."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from gui_agent.core.run.io import (
    LOG_ROOT,
    capture_stdio,
    create_run_dir,
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
        self.log_root = (log_root or LOG_ROOT).expanduser().resolve()

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

        if self.log_root == LOG_ROOT.resolve():
            log_dir = create_run_dir("tool_agent", platform)
        else:
            base = self.log_root / "tool_agent" / platform
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_dir = base / name
            suffix = 2
            while log_dir.exists():
                log_dir = base / f"{name}_{suffix}"
                suffix += 1
            log_dir.mkdir(parents=True)

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

        run_id = str(log_dir.resolve().relative_to(self.log_root))
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

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Read a prior run summary without allowing paths outside the log root."""

        run_dir = (self.log_root / run_id).resolve()
        if self.log_root not in run_dir.parents:
            raise ValueError("run_id resolves outside GUIWeave's log root")
        context_path = run_dir / "context.json"
        if not context_path.is_file():
            raise FileNotFoundError(f"unknown run_id: {run_id}")
        context = json.loads(context_path.read_text(encoding="utf-8"))
        return {
            "run_id": run_id,
            "run_dir": str(run_dir),
            "goal": context.get("goal"),
            "platform": context.get("platform"),
            "outcome": context.get("outcome"),
            "reply": context.get("reply"),
            "orchestrator": context.get("orchestrator"),
            "report_path": (
                str(run_dir / "report.html")
                if (run_dir / "report.html").is_file()
                else None
            ),
        }


__all__ = ["ToolAgentService", "ToolAgentServiceResult"]
