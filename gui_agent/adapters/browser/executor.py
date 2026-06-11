"""Browser action executor: dispatch one ActionDecision onto the page.

Reuses the shared ``gui_agent.core.executor.VisionExecutor`` (the 7 shared actions +
denorm + _tap); only the browser-specific bits live here:
  - ``_clear_before_type`` — select-all for plain <input>/<textarea>, but SKIP it on
    contenteditable rich editors (Cmd+A there enters block-node selection and swallows
    the next type).
  - ``_dispatch_extra`` — navigate / back / new_tab / select_tab / close_tab.
Coordinates are normalized 0-1000 over the viewport; NO YOLO snap, NO picker.
"""

from __future__ import annotations

from typing import Optional

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.core.executor import VisionExecutor


class BrowserExecutor(VisionExecutor):
    """Execute normalized policy actions against the browser via PlaywrightDevice."""

    def _client(self):
        client = getattr(self.session, "client", None)
        if client is None:
            raise RuntimeError("浏览器尚未连接")
        return client

    def execute(self, decision, app_name: str = "", png_bytes=None, is_home_screen: bool = False) -> bool:
        # Stash the action so _tap can record its DOM snap on it (the report / runtime visualizer
        # read action.snap to draw original→snapped, the same as iphone YOLO/OCR).
        self._cur_action = decision.action
        return super().execute(decision, app_name, png_bytes, is_home_screen)

    def _tap(self, px: float, py: float) -> bool:
        """DOM-snap the click point to the clickable element under it, then click. The DOM
        analogue of iphone's YOLO snap (browser only — it has the DOM). Conservative: a no-op
        for canvas / huge containers / non-clickable points (see device.dom_snap), so it only
        fixes a near-miss on a real control and never moves a legitimate precise click.

        Records ``action.snap`` (normalized 0-1000, method 'dom') when it moves the point, so the
        HTML report / runtime visualizer draw the original→snapped correction like YOLO/OCR."""
        sx, sy = px, py
        try:
            cx, cy, info = self._client().dom_snap(px, py)
            if info is not None and (abs(cx - px) > 1 or abs(cy - py) > 1):
                print(f"  DOM 吸附: ({px:.0f},{py:.0f}) → ({cx:.0f},{cy:.0f}) [{info}]")
                sx, sy = cx, cy
                self._record_snap(px, py, sx, sy, info)
        except Exception:  # noqa: BLE001 — never block a click on a snap failure
            pass
        return super()._tap(sx, sy)

    def _record_snap(self, px: float, py: float, sx: float, sy: float, info: str) -> None:
        """Write action.snap in normalized 0-1000 (original=LLM coords, snapped=DOM center)."""
        action = getattr(self, "_cur_action", None)
        if action is None or action.x is None or action.y is None:
            return
        try:
            w, h = self._client().viewport_size
            action.snap = {
                "method": "dom",
                "original": [action.x, action.y],          # LLM's normalized coords
                "snapped": [sx / w * 1000, sy / h * 1000],  # DOM center, normalized back
                "info": info,
            }
        except Exception:  # noqa: BLE001
            pass

    def _clear_before_type(self, client, text: str) -> None:
        # Replace existing contents: select-all + type. BUT only for plain
        # <input>/<textarea>. On a contenteditable rich block editor (Feishu/Notion),
        # Cmd+A on an empty block enters BLOCK-NODE selection and the following type is
        # SWALLOWED — so for contenteditable we skip select-all and type at the cursor
        # (the field is empty in the vast majority of type targets; a genuine replace
        # can emit a separate clear_text).
        kind = _focused_kind(client)
        if kind == "input":
            print(f"  清空并输入: {text!r}")
            print(f"  结果: {client.select_all()}")
        else:
            print(f"  输入（{kind}，跳过 select_all 防块编辑器吞字）: {text!r}")

    def _dispatch_extra(self, action: BrowserAction, client) -> Optional[bool]:
        at = action.action_type
        if at == "navigate":
            url = _normalize_url(action.url)
            if not url:
                print("导航失败：缺少 url")
                return False
            print(f"导航到地址栏: {url}")
            result = client.navigate(url)
            print(f"  结果: {result}")
            return "failed" not in result.lower()
        if at == "back":
            print("浏览器历史后退")
            print(f"  结果: {client.go_back()}")
            return True
        if at == "new_tab":
            url = _normalize_url(action.url) if action.url else None
            print(f"新建标签页{('，导航到 ' + url) if url else ''}")
            print(f"  结果: {client.new_tab(url)}")
            return True
        if at == "select_tab":
            print(f"切换到标签页（匹配 {action.tab_match!r}）")
            print(f"  结果: {client.select_tab(action.tab_match)}")
            return True
        if at == "close_tab":
            print(f"关闭标签页（{action.tab_match or '当前'}）")
            print(f"  结果: {client.close_tab(action.tab_match)}")
            return True
        if at == "upload":
            if not action.file_path or action.x is None or action.y is None:
                print("上传失败：缺少 file_path 或上传控件坐标 x/y")
                return False
            px, py = self._denorm(action.x, action.y)
            print(f"上传文件 {action.file_path} → 点击上传控件 ({px:.0f},{py:.0f})，经 file chooser 送文件")
            result = client.upload_file(px, py, action.file_path)
            print(f"  结果: {result}")
            return "failed" not in result.lower()
        return None


_FOCUS_KIND_JS = """(() => {
    const a = document.activeElement;
    if (!a) return 'none';
    const t = a.tagName;
    if (t === 'INPUT' || t === 'TEXTAREA') return 'input';
    if (a.isContentEditable) return 'ce';
    return 'other';
})()"""


def _focused_kind(client) -> str:
    """Classify the currently-focused element so the type path knows whether a
    select-all (Cmd+A) is safe. 'input' for <input>/<textarea> (select-all reliably
    replaces), 'ce' for contenteditable (select-all swallows the next type — skip),
    'other'/'none' otherwise. Defaults to 'input' if eval is unavailable."""
    ev = getattr(client, "eval_js", None)
    if ev is None:
        return "input"
    try:
        kind = ev(_FOCUS_KIND_JS)
    except Exception:  # noqa: BLE001
        return "input"
    return kind if isinstance(kind, str) and kind else "input"


def _normalize_url(url: str | None) -> str:
    """Prepend https:// when the model gives a bare host (e.g. 'feishu.cn').
    Anything already carrying a scheme — or an about:/chrome:/file: URL — passes through."""
    if not url:
        return ""
    u = url.strip()
    if not u:
        return ""
    if "://" in u or u.startswith("about:") or u.startswith("chrome:"):
        return u
    return f"https://{u}"
