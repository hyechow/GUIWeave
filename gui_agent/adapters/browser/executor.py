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

import json
import math
import re
from typing import Optional
from urllib.parse import urljoin

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control
from gui_agent.core.runtime.executor import VisionExecutor

_DESELECT_OPTION = re.compile(
    r"\b(deselect|unselect|uncheck|remove|toggle off)\b",
    re.IGNORECASE,
)


def _wants_multi_select_deselect(description: object) -> bool:
    """True when the Worker asked to drop one option from a native multi-select."""

    return bool(_DESELECT_OPTION.search(str(description or "")))

# jQuery UI datepicker capability. A non-bubbling ``change`` reaches the
# widget's direct binding without triggering delegated form behavior.
_JQUERY_DATEPICKER_SET_JS = r"""(() => {{
  const el = document.activeElement;
  if (!el || !el.classList.contains('_has-datepicker') || !window.jQuery) return false;
  const raw = {value};
  const iso = raw.match(/^(\d{{4}})-(\d{{2}})-(\d{{2}})$/);
  let date;
  if (iso) {{
    const [, y, m, d] = iso;
    date = new Date(Number(y), Number(m) - 1, Number(d));
  }} else {{
    date = new Date(raw);
  }}
  if (isNaN(date.getTime())) return false;
  try {{
    jQuery(el).datepicker('setDate', date);
  }} catch(e) {{
    return false;
  }}
  el.dispatchEvent(new Event('change', {{bubbles: false}}));
  return {{value: el.value, event: 'change'}};
}})()"""

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

    def execute(
        self,
        decision,
        app_name: str = "",
        png_bytes=None,
        is_home_screen: bool = False,
        target_control: str = "",
    ) -> bool:
        # Stash the action so _tap can record its DOM snap on it (the report / runtime visualizer
        # read action.snap to draw original→snapped, the same as iphone YOLO/OCR).
        self._cur_action = decision.action
        self._cur_target_control = target_control
        self._prepare_scroll_anchor(decision.action)
        begin_feedback = getattr(self._client(), "begin_action_feedback", None)
        if callable(begin_feedback):
            begin_feedback()
        return super().execute(
            decision,
            app_name,
            png_bytes,
            is_home_screen,
            target_control=target_control,
        )

    def execute_scroll(self, action, *, ticks: int = 0, delta_px: int = 0) -> bool:
        """Resolve omitted browser wheel coordinates before dispatch."""
        self._prepare_scroll_anchor(action)
        return super().execute_scroll(action, ticks=ticks, delta_px=delta_px)

    def ground_coordinates(self, decision, controls):
        """Ground one enhanced-mode visual point without exposing DOM identity."""
        try:
            return ground_action_to_nearest_control(
                decision,
                controls,
                viewport_size=self._client().viewport_size,
            )
        except Exception:  # noqa: BLE001 - grounding must fail open to visual coordinates
            return decision

    def _prepare_scroll_anchor(self, action) -> None:
        """Put coordinate-free region/page scrolling on a non-control DOM surface.

        Explicit x/y means the policy intentionally targeted a local scroll container and is left
        untouched. Coordinate-free scrolling is page/region scrolling; using the viewport center
        blindly can land on a form control that consumes the wheel without moving the page.
        """
        if getattr(action, "action_type", "") != "scroll":
            return
        if action.x is not None and action.y is not None:
            return
        client = self._client()
        resolver = getattr(client, "safe_scroll_anchor", None)
        if not callable(resolver):
            return
        try:
            width, height = client.viewport_size
            target_area = getattr(action, "target_area", "main_content") or "main_content"
            area_anchors = {
                "left_panel": (0.25, 0.5),
                "right_panel": (0.75, 0.5),
                "top_content": (0.5, 0.25),
                "bottom_content": (0.5, 0.75),
            }
            x_fraction, y_fraction = area_anchors.get(target_area, (0.5, 0.5))
            preferred_x = width * x_fraction
            preferred_y = height * y_fraction
            resolved = resolver(
                preferred_x,
                preferred_y,
                target_area,
            )
            if resolved is None:
                return
            px, py, info = resolved
            action.x = max(0.0, min(1000.0, px / width * 1000.0))
            action.y = max(0.0, min(1000.0, py / height * 1000.0))
            print(
                f"  [ScrollAnchor] 视口中心 → 非控件落点 ({px:.0f},{py:.0f})"
                + (f" [{info}]" if info else "")
            )
        except Exception:  # noqa: BLE001 — fallback to the neutral center anchor
            return

    def _tap(self, px: float, py: float) -> bool:
        """DOM-snap the click point to the clickable element under it, then click. The DOM
        analogue of iphone's YOLO snap (browser only — it has the DOM). Conservative: a no-op
        for canvas / huge containers / non-clickable points (see device.dom_snap), so it only
        fixes a near-miss on a real control and never moves a legitimate precise click.

        Also passes the typed ActionIntent target so dom_snap can TEXT-RETARGET when the point
        landed on a differently-labelled neighbour. Free-form action prose is never parsed into
        target identity here.

        Records ``action.snap`` (normalized 0-1000, method 'dom') when it moves the point, so the
        HTML report / runtime visualizer draw the original→snapped correction like YOLO/OCR."""
        sx, sy = px, py
        if getattr(self, "disable_dom_snap", False):
            return super()._tap(sx, sy)
        try:
            action = getattr(self, "_cur_action", None)
            at = getattr(action, "action_type", "")
            description = getattr(action, "description", "") or ""
            target = (
                str(getattr(self, "_cur_target_control", "") or "").strip()
                if at in ("tap", "click", "type")
                else ""
            )
            cx, cy, info = self._client().dom_snap(px, py, target_text=target)
            viewport = getattr(self._client(), "viewport_size", None)
            in_viewport = bool(
                isinstance(viewport, tuple)
                and len(viewport) == 2
                and 0 <= cx < viewport[0]
                and 0 <= cy < viewport[1]
            )
            if (
                info is not None
                and in_viewport
                and (abs(cx - px) > 1 or abs(cy - py) > 1)
                and _should_accept_dom_snap(description, info, px, py, cx, cy)
            ):
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

    def _type_intercept(self, client, text: str) -> bool:
        """Commit a focused jQuery UI datepicker through its public widget API.

        This is a narrow adapter capability, not a universal date-control path.
        Unrecognized controls fall back to normal typing or visual interaction.

        Returns True (skip normal clear+type) when a date was set via the API.
        Falls back to normal typing for non-datepicker fields or when jQuery is absent.
        """
        ev = getattr(client, "eval_js", None)
        if ev is None:
            return False
        try:
            result = ev(_JQUERY_DATEPICKER_SET_JS.format(value=json.dumps(text.strip())))
            if isinstance(result, dict) and result.get("value"):
                print(
                    f"  [datepicker] value={result['value']!r} "
                    "committed via local change"
                )
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _clear_before_type(self, client, text: str) -> bool:
        # Replace existing contents: select-all + type. BUT only for plain
        # <input>/<textarea>. On a contenteditable rich block editor (Feishu/Notion),
        # Cmd+A on an empty block enters BLOCK-NODE selection and the following type is
        # SWALLOWED — so for contenteditable we skip select-all and type at the cursor
        # (the field is empty in the vast majority of type targets; a genuine replace
        # can emit a separate clear_text).
        kind = _focused_kind(client)
        displayed_text = (
            "[session access value redacted]"
            if text in getattr(self, "sensitive_text_values", ())
            else repr(text)
        )
        if kind == "input":
            print(f"  清空并输入: {displayed_text}")
            result = client.select_all()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "输入前全选")
        else:
            print(f"  输入（{kind}，跳过 select_all 防块编辑器吞字）: {displayed_text}")
            return True

    def _dispatch_extra(self, action: BrowserAction, client) -> Optional[bool]:
        at = action.action_type
        if at == "navigate":
            url = _normalize_url(action.url, _current_url(client))
            if not url:
                print("导航失败：缺少 url")
                return False
            print(f"导航到地址栏: {url}")
            result = client.navigate(url)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "导航")
        if at == "back":
            print("浏览器历史后退")
            result = client.go_back()
            print(f"  结果: {result}")
            return self._result_succeeded(result, "浏览器后退")
        if at == "new_tab":
            url = (
                _normalize_url(action.url, _current_url(client))
                if action.url
                else None
            )
            print(f"新建标签页{('，导航到 ' + url) if url else ''}")
            result = client.new_tab(url)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "新建标签页")
        if at == "select_tab":
            print(f"切换到标签页（匹配 {action.tab_match!r}）")
            result = client.select_tab(action.tab_match)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "切换标签页")
        if at == "close_tab":
            print(f"关闭标签页（{action.tab_match or '当前'}）")
            result = client.close_tab(action.tab_match)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "关闭标签页")
        if at == "upload":
            if not action.file_path or action.x is None or action.y is None:
                print("上传失败：缺少 file_path 或上传控件坐标 x/y")
                return False
            px, py = self._denorm(action.x, action.y)
            print(f"上传文件 {action.file_path} → 点击上传控件 ({px:.0f},{py:.0f})，经 file chooser 送文件")
            result = client.upload_file(px, py, action.file_path)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "上传")
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
            result = client.select_option(
                px,
                py,
                action.text,
                deselect=_wants_multi_select_deselect(action.description),
            )
            print(f"  结果: {result}")
            return self._result_succeeded(result, "选择下拉选项")
        if at == "scroll_to_ref":
            if action.target_ref is None:
                print("精确滚动失败：缺少 target_ref")
                return False
            print(f"将语义目标 ref={action.target_ref} 移入视口")
            result = client.scroll_to_ref(action.target_ref)
            print(f"  结果: {result}")
            return self._result_succeeded(result, "精确滚动")
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


def _current_url(client: object) -> str:
    page_info = getattr(client, "page_info", None)
    if not callable(page_info):
        return ""
    try:
        current_url, _title = page_info()
    except Exception:  # noqa: BLE001 - URL normalization can use legacy fallback
        return ""
    return str(current_url or "")


def _normalize_url(url: str | None, current_url: str = "") -> str:
    """Prepend https:// when the model gives a bare host (e.g. 'feishu.cn').
    Resolve explicit same-origin relative routes against the current page. Anything
    already carrying a scheme — or an about:/chrome:/file: URL — passes through."""
    if not url:
        return ""
    u = url.strip()
    if not u:
        return ""
    if current_url and u.startswith(("/", "./", "../", "?", "#")):
        return urljoin(current_url, u)
    if "://" in u or u.startswith("about:") or u.startswith("chrome:"):
        return u
    return f"https://{u}"
