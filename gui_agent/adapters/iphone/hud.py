"""iPhone HUD placement: positions the neutral core ``AgentHUD`` below the mirror.

The HUD widget itself is platform-neutral (``gui_agent.core.hud``); this module
only computes WHERE iPhone wants it (below the 318x701 mirror window) and keeps the
historical look (opaque, alpha=1.0). ``AgentHUD`` is re-exported so existing
``from ...iphone.hud import AgentHUD`` type references keep working.
"""

from gui_agent.core.hud import AgentHUD

__all__ = ["AgentHUD", "make_iphone_hud"]


def make_iphone_hud() -> AgentHUD:
    """Construct the HUD positioned just below the iPhone Mirroring window."""
    from gui_agent.adapters.iphone.executor import _find_iphone_window

    pos = _find_iphone_window()
    if pos:
        ox, oy = int(pos[0]), int(pos[1]) + 701 + 2
    else:
        ox, oy = 100, 100
    return AgentHUD(origin=(ox, oy), width=318, height=150, alpha=1.0)
