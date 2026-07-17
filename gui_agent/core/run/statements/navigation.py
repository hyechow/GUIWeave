"""Deterministic browser-navigation fast path for interactive Run statements."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from gui_agent.core.orchestrator.program import Run, RunLike


from .observation import ObservationCursor
from .outcome import StatementOutcome


_URL_RE = re.compile(r"https?://[^\s一-鿿]+")
_BACK_NAV_RE = re.compile(r"返回|上一页|后退|\bback\b", re.IGNORECASE)


def _url_identity(value: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(value)
    return (
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        parsed.path.rstrip("/") or "/",
        parsed.query,
    )


def direct_navigation_url(statement: RunLike | None, platform: Any) -> str | None:
    """Return a concrete navigation target when the platform can execute it directly."""
    if statement is None or statement.kind != "navigation":
        return None
    if not callable(getattr(getattr(platform, "client", None), "navigate", None)):
        return None
    match = _URL_RE.search(statement.name or "")
    if not match:
        return None
    return match.group(0).rstrip(").,;，。）") or None


def is_direct_back(statement: RunLike | None, platform: Any) -> bool:
    """Whether a navigation can use deterministic browser history/back capability."""
    if statement is None or statement.kind != "navigation":
        return False
    if not callable(getattr(getattr(platform, "client", None), "go_back", None)):
        return False
    return bool(_BACK_NAV_RE.search(statement.name or ""))


def can_execute_navigation_immediately(statement: RunLike | None, platform: Any) -> bool:
    return direct_navigation_url(statement, platform) is not None or is_direct_back(statement, platform)


def _settle(client: Any, reason: str) -> None:
    settle = getattr(client, "wait_settled", None)
    if callable(settle):
        try:
            settle(reason)
        except Exception:  # noqa: BLE001 - settling is best-effort; observe regardless
            pass


def execute_direct_navigation(
    run: Run,
    *,
    statement_index: int,
    sequence: int,
    return_stack: list[str],
    cursor: ObservationCursor,
    bundle: Any,
    platform: Any,
    log_dir: Path,
    check_knowledge: str,
    say: Callable[[str], None],
    status: Callable[[str], None],
) -> StatementOutcome:
    """Execute one concrete URL/back command and observe the landed page."""
    reads: dict[str, str] = {}
    context_reports: list[dict] = []

    if is_direct_back(run, platform):
        return_url = return_stack.pop() if return_stack else ""
        if return_url:
            status(f"直达返回 {sequence}：回到 {return_url}")
            say(f"  [Orchestrator] 直达返回 {sequence} · 导航回 {return_url}")
            platform.client.navigate(return_url)
        else:
            status(f"直达返回 {sequence}：浏览器后退")
            say(f"  [Orchestrator] 直达返回 {sequence} · 浏览器后退")
            platform.client.go_back()
        _settle(platform.client, "navigate" if return_url else "back")
        cursor.refresh(f"screenshot_back_{statement_index}.png")
        summary = "浏览器后退"
    else:
        from gui_agent.core.orchestrator.primitives.url_json_read import read_json_url_returns

        nav_url = direct_navigation_url(run, platform)
        assert nav_url is not None
        if cursor.observation is not None and getattr(cursor.observation, "url", None):
            return_stack.append(str(cursor.observation.url))
        status(f"直达钻取 {sequence}：打开 {nav_url}")
        say(f"  [Orchestrator] 直达钻取 {sequence} · 直达导航 {nav_url}")
        platform.client.navigate(nav_url)
        _settle(platform.client, "navigate")
        observation = cursor.refresh(f"screenshot_nav_{statement_index}.png")
        if observation.url and _url_identity(observation.url) != _url_identity(nav_url):
            return StatementOutcome.failed(
                f"直达导航落在错误目标：{observation.url!r} != {nav_url!r}",
                observation=observation,
                observation_url=cursor.observation_url,
                failure_evidence=(
                    f"navigation target mismatch: expected {nav_url!r}, "
                    f"observed {observation.url!r}"
                ),
            )

        if run.returns:
            json_reads = read_json_url_returns(run.name, list(run.returns), run.read_spec)
            if json_reads is not None and any(
                str(json_reads.get(field, "")).strip() for field in run.returns
            ):
                reads = json_reads
                say(f"  [Orchestrator] 直达后 URL JSON 读取 {run.returns} → {reads}")
            else:
                from gui_agent.adapters.browser.page_read import read_page_complete

                reads = read_page_complete(
                    observation,
                    list(run.returns),
                    read_spec=run.read_spec,
                    check_knowledge=check_knowledge,
                    bundle=bundle,
                    platform=platform,
                    log_dir=log_dir,
                    context_reports=context_reports,
                )
                say(f"  [Orchestrator] 直达后读取 {run.returns} → {reads}")
        summary = f"直达导航 {nav_url}"

    return StatementOutcome.completed(
        summary,
        verification="confirmed",
        reads=reads,
        observation=cursor.observation,
        observation_url=cursor.observation_url,
        context_reports=context_reports,
    )
