"""Browser action executor: dispatch one ActionDecision onto the page.

Reuses the shared ``gui_agent.core.runtime.executor.VisionExecutor`` (the 7 shared actions +
denorm + _tap); only the browser-specific bits live here:
  - ``_clear_before_type`` — select-all for plain <input>/<textarea>, but SKIP it on
    contenteditable rich editors (Cmd+A there enters block-node selection and swallows
    the next type).
  - ``_dispatch_extra`` — navigate / back / new_tab / select_tab / close_tab.
Coordinates are normalized 0-1000 over the viewport; NO YOLO snap, NO picker.
"""

from __future__ import annotations

import re
from typing import Optional

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.core.runtime.executor import VisionExecutor

# Quoted UI label in an action description: 「操作」 / 『确定』 / "取消" / '编辑'.
_QUOTE_RE = re.compile(r"[「『\"']([^「」『』\"']{1,8})[」』\"']")
_INLINE_EN_LABEL_RE = re.compile(
    r"(?:菜单下的|子菜单中的|下的|中的)\s*([A-Z][A-Za-z0-9 &_-]{1,40})\s*"
    r"(?:选项|菜单项|菜单|按钮|链接)"
    r"|点击\s*([A-Z][A-Za-z0-9 &_-]{1,40})\s*(?:选项|菜单项|菜单|按钮|链接)"
)
_OPTION_QUOTE_RE = re.compile(r"[「『\"']([^「」『』\"']{1,40})[」』\"']")
_OPTION_VALUE_RE = re.compile(r"(?:=|为|to)\s*([A-Za-z][\w ._/-]{0,39})", re.IGNORECASE)
_OPTION_AFTER_SELECT_RE = re.compile(
    r"(?:选择|选中|select)\s*([A-Za-z][\w ._/-]{0,39})",
    re.IGNORECASE,
)
_SELECT_INTENT_RE = re.compile(r"选择|选中|设为|设置|下拉.*选项|select\s+option", re.IGNORECASE)


def _quoted_label(description: str) -> str:
    """The LAST short quoted label in the description — the actionable target
    (「点击…菜单中的「操作」」→ 操作). Longer quotes (robot names, option values with
    underscores) are skipped by the 8-char cap: those targets are unique under the
    point anyway, while short labels (操作/删除/确定/取消) are the ambiguous ones."""
    matches = _QUOTE_RE.findall(description or "")
    return matches[-1] if matches else ""


def _target_label(description: str) -> str:
    """Best-effort clickable label for DOM text retarget.

    Prefer explicit quotes. As a browser-specific fallback, extract short English admin UI
    labels from common Chinese action phrasing, e.g. "Sales 菜单下的 Orders 选项" or
    "点击 Filters 按钮". This rescues visible menu-row misses without treating arbitrary
    long Chinese instruction text as a label.
    """
    quoted = _quoted_label(description)
    if quoted:
        return quoted
    match = _INLINE_EN_LABEL_RE.search(description or "")
    if not match:
        return ""
    label = next((g for g in match.groups() if g), "")
    return label.strip()[:40]


def _select_option_label(description: str) -> str:
    """Option text for a native select rescue.

    Unlike _quoted_label(), option values can be longer than short UI labels. Keep this
    extraction gated by select intent so a plain "click Status" tap never tries to set
    the select value to "Status".
    """
    text = description or ""
    if not _SELECT_INTENT_RE.search(text):
        return ""
    matches = _OPTION_QUOTE_RE.findall(text)
    if matches:
        return matches[-1].strip()
    match = _OPTION_VALUE_RE.search(text)
    if match:
        return match.group(1).strip(" .,;，。；")
    match = _OPTION_AFTER_SELECT_RE.search(text)
    if match:
        value = match.group(1).strip(" .,;，。；")
        return "" if value.lower() in {"option", "options"} else value
    return ""


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

        Also passes the label quoted in the action description (e.g. 「操作」) so dom_snap can
        TEXT-RETARGET when the point landed on a differently-labelled neighbour — the OCR-snap
        analogue. Rescues adjacent-menu-item misses: 操作/删除 are 28px apart, the vision
        model's y was one row off, and the click hit the DESTRUCTIVE neighbour 3× in a row
        (run 20260612_114219).

        Records ``action.snap`` (normalized 0-1000, method 'dom') when it moves the point, so the
        HTML report / runtime visualizer draw the original→snapped correction like YOLO/OCR."""
        sx, sy = px, py
        try:
            action = getattr(self, "_cur_action", None)
            # Text-retarget is a TAP-on-labelled-control rescue (the 操作/删除 neighbour miss).
            # Only pass the quoted label for a genuine tap/click. For a `type` focus-tap the
            # quoted string in the description is the VALUE being typed, NOT a UI label — and
            # dom_snap matches inputs by their .value, so it wrongly snaps onto an already-filled
            # field holding that same value (run 20260613_193023: typing 'admin' into the password
            # box kept retargeting to the account box whose value was already 'admin' → login stuck).
            at = getattr(action, "action_type", "")
            target = _target_label(getattr(action, "description", "") or "") if at in ("tap", "click") else ""
            cx, cy, info = self._client().dom_snap(px, py, target_text=target)
            if info is not None and (abs(cx - px) > 1 or abs(cy - py) > 1):
                print(f"  DOM 吸附: ({px:.0f},{py:.0f}) → ({cx:.0f},{cy:.0f}) [{info}]")
                sx, sy = cx, cy
                self._record_snap(px, py, sx, sy, info)
            option_text = _select_option_label(getattr(action, "description", "") or "")
            if at in ("tap", "click") and option_text and (info or "").startswith("select "):
                print(f"  原生下拉选择: {option_text!r}")
                result = self._client().select_option(sx, sy, option_text)
                print(f"  结果: {result}")
                return "failed" not in result.lower()
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
        if at == "select_option":
            if not action.text:
                print("选择下拉选项失败：缺少 text")
                return False
            px = py = None
            if action.x is not None and action.y is not None:
                px, py = self._denorm(action.x, action.y)
                print(f"选择下拉选项 {action.text!r} @({px:.0f},{py:.0f})")
            else:
                print(f"选择当前聚焦下拉选项 {action.text!r}")
            result = client.select_option(px, py, action.text)
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
