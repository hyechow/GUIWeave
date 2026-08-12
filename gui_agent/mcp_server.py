"""Local stdio MCP server for GUIWeave's Tool Agent runtime."""

from __future__ import annotations

from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from gui_agent.core.tool_agent.service import ToolAgentService
from gui_agent.core.runtime.platforms import PlatformName
from gui_agent.core.self_learning.document_ingest import KnowledgeImportService


load_dotenv()

server = FastMCP(
    "guiweave-automation",
    instructions=(
        "Use check_environment before the first run on a platform. Browser, Android, "
        "and iPhone tasks operate the user's local GUI and may change external state. "
        "Preserve the user's exact goal and report the returned run_id and artifacts. "
        "Document import must be previewed and explicitly confirmed in a later user "
        "turn before committing private knowledge."
    ),
)
service = ToolAgentService()
knowledge_service = KnowledgeImportService()


def _options(
    platform: PlatformName,
    *,
    cdp_url: str | None = None,
    adb_serial: str | None = None,
    headless: bool = False,
) -> dict[str, object]:
    if platform == "browser":
        return {"cdp_url": cdp_url, "headless": headless}
    if platform == "android":
        return {"serial": adb_serial}
    return {}


@server.tool(
    title="Check GUI automation environment",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def check_environment(
    platform: PlatformName,
    cdp_url: str | None = None,
    adb_serial: str | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """Check whether local Browser, Android, or iPhone automation is ready.

    Android preflight may activate ADBKeyboard so non-ASCII text input works.
    """

    result = service.check_environment(
        platform,
        **_options(
            platform,
            cdp_url=cdp_url,
            adb_serial=adb_serial,
            headless=headless,
        ),
    )
    return {
        "ok": result.ok,
        "summary": result.summary,
        "details": list(result.lines),
    }


@server.tool(
    title="Run a local browser task",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    ),
)
def run_browser_task(
    goal: str,
    cdp_url: str | None = None,
    headless: bool = False,
    perception: Literal["vision-only", "enhanced"] = "enhanced",
    max_turns: int = 50,
    multi_action: bool = True,
    show_hud: bool = True,
) -> dict[str, Any]:
    """Run an exact user-authorized goal in local Chrome.

    This broad tool may navigate, submit forms, send data or otherwise change
    external state. Confirm consequential intent with the user before calling it.
    """

    return service.run(
        goal,
        platform="browser",
        perception_mode=perception,
        max_turns=max_turns,
        allow_multi_action=multi_action,
        show_hud=show_hud and not headless,
        mirror_stdio=False,
        cdp_url=cdp_url,
        headless=headless,
    ).to_dict()


@server.tool(
    title="Run a local Android task",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    ),
)
def run_android_task(
    goal: str,
    adb_serial: str | None = None,
    perception: Literal["vision-only", "enhanced"] = "enhanced",
    max_turns: int = 50,
    multi_action: bool = True,
    show_hud: bool = True,
) -> dict[str, Any]:
    """Run an exact user-authorized goal on a locally connected Android device.

    This broad tool may send messages, alter settings or otherwise change external
    state. Confirm consequential intent with the user before calling it.
    """

    return service.run(
        goal,
        platform="android",
        perception_mode=perception,
        max_turns=max_turns,
        allow_multi_action=multi_action,
        show_hud=show_hud,
        mirror_stdio=False,
        serial=adb_serial,
    ).to_dict()


@server.tool(
    title="Run a local iPhone task",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=True,
    ),
)
def run_iphone_task(
    goal: str,
    perception: Literal["vision-only", "enhanced"] = "enhanced",
    max_turns: int = 50,
    multi_action: bool = True,
) -> dict[str, Any]:
    """Run an exact user-authorized goal through macOS iPhone Mirroring.

    Screenshots come only from bin/sck_server. Input is sent only through
    bin/mirror_daemon. Confirm consequential intent before calling this tool.
    """

    return service.run(
        goal,
        platform="iphone",
        perception_mode=perception,
        max_turns=max_turns,
        allow_multi_action=multi_action,
        show_hud=False,
        mirror_stdio=False,
    ).to_dict()


@server.tool(
    title="Get a GUIWeave run",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def get_run_result(run_id: str) -> dict[str, Any]:
    """Read the summary and artifact paths for a previous GUIWeave run."""

    return service.get_run(run_id)


@server.tool(
    title="Preview a document as GUIWeave knowledge",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def preview_knowledge_document(
    file_path: str,
    platform: PlatformName,
    app_name: str | None = None,
) -> dict[str, Any]:
    """Convert a local PDF, Markdown or text manual into a reviewable draft.

    The source document is treated as untrusted data. This creates only a private
    draft; it does not make the knowledge active. Scanned PDFs require OCR first.
    """

    return knowledge_service.preview_document(
        file_path,
        platform=platform,
        app_name=app_name,
    )


@server.tool(
    title="Read a GUIWeave knowledge draft",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def get_knowledge_draft(draft_id: str) -> dict[str, Any]:
    """Read the generated files for a pending or committed knowledge draft."""

    return knowledge_service.get_draft(draft_id)


@server.tool(
    title="Commit a confirmed GUIWeave knowledge draft",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        openWorldHint=False,
    ),
)
def commit_knowledge_draft(
    draft_id: str,
    confirmation_token: str,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    """Activate a previewed knowledge draft after explicit user confirmation.

    Never call this in the same turn that created the preview. Show the generated
    files first and wait for a subsequent user message that explicitly approves them.
    Overwriting existing knowledge requires separately explicit user authorization.
    """

    return knowledge_service.commit_draft(
        draft_id,
        confirmation_token,
        overwrite_existing=overwrite_existing,
    )


@server.tool(
    title="List user-installed GUIWeave knowledge",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def list_user_knowledge(
    platform: PlatformName | None = None,
) -> dict[str, Any]:
    """List private knowledge committed from user documents."""

    return knowledge_service.list_knowledge(platform)


@server.tool(
    title="Read user-installed GUIWeave knowledge",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
    ),
)
def get_user_knowledge(
    platform: PlatformName,
    app_slug: str,
) -> dict[str, Any]:
    """Read the active private knowledge files for one application."""

    return knowledge_service.get_knowledge(platform, app_slug)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
