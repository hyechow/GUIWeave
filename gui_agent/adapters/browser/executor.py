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

import math
import re
from typing import Optional

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.adapters.browser.option_text import option_text_from_instruction
from gui_agent.core.runtime.executor import VisionExecutor

# Quoted UI label in an action description: 「操作」 / 『确定』 / "取消" / '编辑'.
_QUOTE_RE = re.compile(r"[「『\"']([^「」『』\"']{1,8})[」』\"']")
_INLINE_EN_LABEL_RE = re.compile(
    r"(?:菜单下的|子菜单中的|下的|中的|内的)\s*([A-Z][A-Za-z0-9 &_-]{1,40})\s*"
    r"(?:选项|菜单项|菜单|按钮|链接|复选框|checkbox)"
    r"|点击\s*([A-Z][A-Za-z0-9 &_-]{1,40})\s*(?:选项|菜单项|菜单|按钮|链接|复选框|checkbox)"
)
_RANGE_FIELD_LABEL_RE = re.compile(
    # The from/to/min/max qualifier must be a SEPARATE word (space before, non-letter after) —
    # otherwise "admin" matches its trailing "min", "tomato" its "to", etc. The field label is a
    # single token (no internal space) so a leading fill verb ("set Quantity to") isn't swallowed
    # into it — Magento's numeric range fields are all single words (Quantity/Price/Weight).
    r"([A-Za-z][\w/-]{0,30}?)\s+(from|to|min|max)(?![A-Za-z])",
    re.IGNORECASE,
)


def _range_field_label(description: str) -> str:
    """The field label a range-filter fill names (e.g. "Quantity to") — used to DOM-snap
    a `type` focus-tap onto the right one of two visually-identical adjacent From/To inputs
    by field identity + from/to role, instead of by the ambiguous vision pixel.

    Only the LABEL is returned, never the typed value (which lives in action.text), so passing
    it to dom_snap cannot recreate the value-retarget bug. Empty for an ordinary type action
    (no from/to/min/max qualifier) — those snap normally with no retarget."""
    match = _RANGE_FIELD_LABEL_RE.search(description or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", f"{match.group(1)} {match.group(2)}").strip()


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
    if quoted and _quoted_label_is_click_target(description, quoted):
        return quoted
    # Values embedded in instructions like "apply 'Olivia' filter" or
    # "search 'Olivia'" are data, not clickable labels. Passing them to DOM
    # text-retarget can snap a nearby icon click back into the input field.
    match = _INLINE_EN_LABEL_RE.search(description or "")
    if not match:
        return ""
    label = next((g for g in match.groups() if g), "")
    return label.strip()[:40]


def _quoted_label_is_click_target(description: str, label: str) -> bool:
    """Return True when a short quoted string is likely a clickable UI label.

    The last short quote in an instruction is often an input/search value
    ("应用 'Olivia' 筛选条件"), not the thing to click. Text-retargeting to such
    values is unsafe because form inputs expose their current value as text.
    """
    text = description or ""
    if not label:
        return False
    quoted_forms = [f"「{label}」", f"『{label}』", f"\"{label}\"", f"'{label}'"]
    positions = [(q, text.rfind(q)) for q in quoted_forms if text.rfind(q) >= 0]
    if not positions:
        return False
    q, pos = max(positions, key=lambda item: item[1])
    before = text[max(0, pos - 16):pos]
    after = text[pos + len(q):pos + len(q) + 16]
    if re.search(r"(输入|搜索|筛选|过滤|关键词|关键字|应用|匹配|包含|值为|设为|设置为)\s*$", before):
        return False
    if re.search(r"^\s*(筛选条件|过滤条件|关键词|关键字|搜索词|查询词|值|文本|文字)", after):
        return False
    if re.search(r"^\s*(按钮|链接|菜单|菜单项|选项|标签|页签|图标|列|控件)", after):
        return True
    if re.search(r"(中的|下的|旁边的|列|按钮|链接|菜单)\s*$", before):
        return True
    return False


def _should_accept_dom_snap(description: str, info: str, px: float, py: float, cx: float, cy: float) -> bool:
    """Reject unsafe DOM snaps for icon clicks.

    Search/clear icons are often rendered inside a text input wrapper. Snapping
    an icon click to the input/text center moves the click away from the icon and
    turns it into a focus no-op.

    Also rejects snaps where the original click point lies outside the snapped
    element's bbox AND the snap distance exceeds 80px — this catches the Magento
    admin flyout failure: clicking inside a flyout panel at (230,247) causes
    .closest('li') to bubble up to the sidebar Catalog <li> (static bbox 88x62,
    center 44,230), which is a DOM ancestor of the absolutely-positioned flyout
    but visually 187px away.  Text-retarget snaps (info starts with 'text ') are
    intentionally far-reaching and exempt from this check.
    """
    if not info:
        return False
    desc = description or ""
    if re.search(r"(图标|icon|放大镜|搜索按钮|search button)", desc, re.IGNORECASE):
        tag = info.split(" ", 1)[0].lower()
        if tag in {"input", "textarea", "text"} and abs(cx - px) > 8:
            return False
    if not info.startswith("text "):
        try:
            parts = info.split(" ", 1)
            if len(parts) > 1:
                w, h = (int(v) for v in parts[1].split("x"))
                tol = 20
                x_ok = (cx - w / 2 - tol) <= px <= (cx + w / 2 + tol)
                y_ok = (cy - h / 2 - tol) <= py <= (cy + h / 2 + tol)
                if not (x_ok and y_ok) and math.hypot(cx - px, cy - py) > 80:
                    return False
        except Exception:
            pass
    return True


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
            description = getattr(action, "description", "") or ""
            if at in ("tap", "click"):
                target = _target_label(description)
            elif at == "type":
                # A range-filter fill (From/To) renders two visually-identical adjacent inputs;
                # vision returns near-identical coords for both, so the focus-tap collapses onto
                # one box. Pass the field LABEL the planner named (carried in description) so
                # dom_snap disambiguates by DOM identity + from/to role. Value stays in
                # action.text → no value-retarget regression.
                target = _range_field_label(description)
            else:
                target = ""
            cx, cy, info = self._client().dom_snap(px, py, target_text=target)
            if (
                info is not None
                and (abs(cx - px) > 1 or abs(cy - py) > 1)
                and _should_accept_dom_snap(description, info, px, py, cx, cy)
            ):
                print(f"  DOM 吸附: ({px:.0f},{py:.0f}) → ({cx:.0f},{cy:.0f}) [{info}]")
                sx, sy = cx, cy
                self._record_snap(px, py, sx, sy, info)
            option_text = option_text_from_instruction(getattr(action, "description", "") or "")
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
