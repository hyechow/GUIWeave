"""iPhone 镜像的输入/截图后端客户端。

- SyncMCPClient:mirroir-mcp 后端(抢占原版,移真光标 + 短暂抢焦点)。
- MirrorDaemonClient:bin/mirror_daemon 后端(零抢占 SkyLight/AX) + agent 光标 overlay。

两者暴露同一 surface(tap / type_text / screenshot / press_home / ...),可互换。
MirrorDaemonClient 缺依赖(agent_cursor/Quartz 等)时容错为 None,不影响 mirroir 后端。
"""
from gui_agent.adapters.iphone.client.sync_mcp import SyncMCPClient, text_content

try:
    from gui_agent.adapters.iphone.client.mirror_daemon import MirrorDaemonClient
except Exception:  # noqa: BLE001 — daemon 后端不可用不应拖垮 mirroir
    MirrorDaemonClient = None  # type: ignore[assignment,misc]

__all__ = ["SyncMCPClient", "MirrorDaemonClient", "text_content"]
