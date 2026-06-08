"""iPhone ReconNavigator: the platform navigation surface the explore loop needs.

Implements the neutral ``gui_agent.core.contracts.ReconNavigator`` by DELEGATING to
the existing recon nav functions (``back_nav`` / ``planned_back_nav`` / ``popup_nav``)
— zero behaviour change. This is the Step 5a seam: today dfs/bfs import those
functions directly; once the explore loop is lifted to core (Step 5b) it will call
an injected ``ReconNavigator`` instead, so a browser navigator (browser back /
history / modal dismissal) can plug in without touching the loop.

Imports are lazy (inside each method) so importing this module stays light and pulls
in no heavy recon deps until a method actually runs.
"""

from __future__ import annotations

from typing import Callable, Optional
from pathlib import Path


class IPhoneReconNavigator:
    """ReconNavigator backed by the iphone recon functions (back/​popup/​recover)."""

    def make_nav_context(self, label: str, element_type: str) -> str:
        from gui_agent.adapters.iphone.recon.back_nav import make_nav_context
        return make_nav_context(label, element_type)

    def return_to_initial(
        self,
        client,
        screenshot: Callable[[], bytes],
        nav_stack: list,
        before_back_bytes: Optional[bytes] = None,
        out_dir: Optional[Path] = None,
        tap_index: int = 0,
        nav_context: str = "",
        status_cb: Optional[Callable[[str], None]] = None,
        selected_tab: str = "",
    ) -> tuple[bool, list]:
        from gui_agent.adapters.iphone.recon.planned_back_nav import planned_return_to_initial
        return planned_return_to_initial(
            client, screenshot, nav_stack,
            before_back_bytes=before_back_bytes, out_dir=out_dir, tap_index=tap_index,
            nav_context=nav_context, status_cb=status_cb, selected_tab=selected_tab,
        )

    def close_popup(
        self,
        client,
        screenshot_fn: Callable[[], bytes],
        current_png: Optional[bytes] = None,
        threshold: float = 0.25,
    ) -> bool:
        from gui_agent.adapters.iphone.recon.popup_nav import close_popup
        return close_popup(client, screenshot_fn, current_png, threshold)

    def manual_recover(
        self,
        client,
        screenshot: Callable[[], bytes],
        nav_stack: list,
        top_level: int,
        prompt: str = "",
        max_attempts: int = 3,
    ) -> bool:
        from gui_agent.adapters.iphone.recon.back_nav import manual_recover
        return manual_recover(client, screenshot, nav_stack, top_level, prompt, max_attempts)
