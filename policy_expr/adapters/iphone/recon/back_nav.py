"""Back-navigation: four-strategy tap with internal YOLO fallback and stack-based assessment."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.core.config import resolve_llm_config
from policy_expr.adapters.iphone.executor import is_valid_tap
from policy_expr.core.policies.base import resize_to_logical_png
from policy_expr.adapters.iphone.recon.page_compare import PageComparator, make_comparator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BACK_TAP_CENTER = (70.0, 125.0)
BACK_TAP_MAX_DIST = 150.0
BACK_SETTLE_SECONDS = 2.0
_PIXEL_DIFF_THRESHOLD = 0.05  # pixel diff >= 5% 视为有变化

# Two comparators with different responsibilies:
# - _change_comp: edge IoU — fast no_change detection inside _try_tap
# - _identity_comp: CascadeMatcher (GUIClip + semantic) — page identity via get_matcher() singleton
#   Higher same_page_threshold: identity matching requires strong visual match (0.70+),
#   unlike navigation detection where 0.20 suffices to separate "navigated" from "maybe same".
_change_comp: PageComparator = make_comparator("edge_iou")
_identity_comp: PageComparator = make_comparator("cascade", same_page_threshold=0.90)


def _get_change_comp() -> PageComparator:
    return _change_comp


def _get_identity_comp() -> PageComparator:
    return _identity_comp


def _pixel_diff_ratio(png_a: bytes, png_b: bytes, threshold: int = 30) -> float:
    """Return ratio of pixels that differ. 0.0 = identical."""
    import io
    import numpy as np
    from PIL import Image
    a = np.array(Image.open(io.BytesIO(png_a)).convert("RGB"))
    b = np.array(Image.open(io.BytesIO(png_b)).convert("RGB"))
    if a.shape != b.shape:
        return 1.0
    diff = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return float((diff > threshold).sum()) / diff.size


# ---------------------------------------------------------------------------
# LLM models
# ---------------------------------------------------------------------------

class BackAction(BaseModel):
    can_go_back: bool = Field(description="能否找到返回到前一页的方法")
    page_type: str = Field(description="导航类型：A=弹窗/浮层, B=tab切换, C=普通页面跳转。⚠️若触发操作含tab但该tab名不在页面tab栏选项中，必须填C")
    method: str = Field(description="返回方法描述")
    back_x: float = Field(default=-1, description="返回目标归一化 x 坐标（0-1000）")
    back_y: float = Field(default=-1, description="返回目标归一化 y 坐标（0-1000）")


BACK_PROMPT = """\
你是一个 iPhone 应用导航专家。根据两张截图（BEFORE、AFTER）和触发操作描述，判断如何从 AFTER 返回 BEFORE。

## 坐标系
左上角 (0,0)，右下角 (1000,1000)，坐标为目标元素视觉中心。
iPhone 导航栏返回按钮通常位于左上角（x<200，y<250）。

## 判断流程（按顺序执行，满足即停）

### Q1：AFTER 有浮层覆盖吗？
检查 AFTER 全屏：是否有弹窗、对话框、底部弹出面板或广告浮层叠在底层页面上方？

浮层的识别依据：底层页面内容仍部分可见；浮层有独立边框/阴影/遮罩；有 ×/关闭/跳过/取消 控件。

**→ 是：page_type = "A"，点击浮层的关闭控件，不需要参考触发操作。**
→ 否：进入 Q2。

### Q2：某一排 tab 栏的选中项发生了变化吗？
iPhone 应用通常同时存在两类 tab 栏，须分开判断：
- **底部导航 tab**：3-5 个固定入口（首页/消息/我/购物车等），在不同页面间通常保持不变
- **顶部分类 tab**：内容分类标签（推荐/热门/关注等），随内容区切换而变化，坐标在屏幕上方（y < 400）

逐排对比，对每一排检查以下三点是否**同时**成立：
1. 该排在 BEFORE 和 AFTER 中均存在
2. 该排的高亮/选中项在两图中**确实不同**（两图相同则跳过该排）
3. 触发操作的名称能在该排选项里找到

**→ 某一排同时满足三点：page_type = "B"，在 AFTER 中点击该排里 BEFORE 所选中的 tab，坐标须落在该排内。**
→ 所有排均不满足：进入 Q3。

注意：不要点击 AFTER 中已处于选中状态的 tab；触发操作含「tab」关键词不代表条件 3 自动满足，须逐字核实。

### Q3：点左上角返回按钮
**→ page_type = "C"，点击 AFTER 左上角的 iPhone 系统返回箭头（‹）或应用内关闭按钮（×）。**

"""


# ---------------------------------------------------------------------------
# Tap operations
# ---------------------------------------------------------------------------

def make_nav_context(label: str, element_type: str) -> str:
    """Build the nav_context string passed to infer_back_action."""
    if element_type == "tab":
        return f"点击了tab「{label}」"
    return f"点击了{element_type}「{label}」"


def _sanitize_nav_context(nav_context: str, after_png: bytes) -> str:
    """Rewrite nav_context when the 'tab' label is misleading.

    When nav_context says "点击了tab「X」" but X is NOT a label found in any
    tab bar of the AFTER screenshot, rewrite to neutral wording so the LLM
    doesn't blindly default to type B.

    Checks both the bottom strip (bottom nav tabs) and the top strip (top
    category tabs, below the status bar) via OCR.  If the tab name appears in
    either region it is a real tab bar label and we keep the original context.
    """
    import re

    m = re.search(r"点击了tab「(.+?)」", nav_context)
    if not m:
        return nav_context
    tab_name = m.group(1)

    try:
        import io
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(after_png))
        w, h = img.size
        # Bottom strip: bottom nav tab bar (bottom 12%)
        bottom_strip = img.crop((0, int(h * 0.88), w, h))
        # Top strip: top category tab bar, below status bar (5%–20%)
        top_strip = img.crop((0, int(h * 0.05), w, int(h * 0.20)))

        combined_text = (
            pytesseract.image_to_string(bottom_strip, lang="chi_sim+eng")
            + pytesseract.image_to_string(top_strip, lang="chi_sim+eng")
        )
        if tab_name in combined_text:
            return nav_context
    except Exception:
        pass  # OCR unavailable or failed → fall through to rewrite

    # Tab name NOT found in any tab bar region → rewrite to remove type B bias
    return f"点击了「{tab_name}」"


def tap_back(client) -> tuple[float, float, str]:
    """Tap the iOS back button area."""
    from policy_expr.adapters.iphone.executor import logical_xy

    lx, ly = logical_xy(*BACK_TAP_CENTER)
    result = client.tap(lx, ly)
    return lx, ly, result


def tap_llm_back(client, action: BackAction) -> tuple[float, float, str] | None:
    """Tap the return target selected by the vision model. Returns None if coords invalid."""
    from policy_expr.adapters.iphone.executor import logical_xy

    if not is_valid_tap(action.back_x, action.back_y):
        return None
    lx, ly = logical_xy(action.back_x, action.back_y)
    result = client.tap(lx, ly)
    return lx, ly, result



# ---------------------------------------------------------------------------
# LLM back action inference
# ---------------------------------------------------------------------------

def _format_elements_context(elements: list[dict]) -> str:
    """Format element list for inclusion in the LLM back-nav prompt."""
    if not elements:
        return ""
    lines = ["以下是AFTER页面预检测到的可交互元素坐标（供参考，最终以视觉判断为准）："]
    for el in elements:
        label = el.get("label") or ""
        etype = el.get("element_type", "")
        x, y = el.get("x", 0), el.get("y", 0)
        leads_to = el.get("leads_to", "")
        label_part = f" {label}" if label else ""
        desc = f" → {leads_to}" if leads_to else ""
        lines.append(f"- [{etype}]{label_part} @ ({x}, {y}){desc}")
    return "\n".join(lines)


def infer_back_action(before_png: bytes, after_png: bytes | None, nav_context: str = "",
                      target_label: str = "",
                      failed_attempts: list[dict] | None = None,
                      after_elements: list[dict] | None = None,
                      debug_fn: Callable | None = None,
                      debug_path: Path | None = None) -> BackAction | None:
    """Ask the vision model how to navigate from AFTER back to BEFORE.

    Args:
        nav_context: How the user navigated from BEFORE to AFTER
                     (e.g. "点击了tab「发现」", "点击了icon「珍珠」").
        after_elements: Pre-parsed interactive elements on the AFTER page.
                        When provided, appended to prompt to ground LLM output.
        debug_fn: Optional callback ``fn(debug_path, context_text, before_b64,
                 after_b64, response_dict)`` for visualizing the LLM call.
        debug_path: Path passed through to *debug_fn*.
    """
    if not after_png:
        return None

    cfg = resolve_llm_config("back_nav")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )
    before_b64 = base64.b64encode(resize_to_logical_png(before_png)).decode()
    after_annotated, back_icon_coord = _annotate_back_icons(after_png)
    after_b64 = base64.b64encode(resize_to_logical_png(after_annotated)).decode()

    context_text = (
        "第一张(BEFORE)是点击前的页面，第二张(AFTER)是点击后跳转的页面。"
        "请找出从 AFTER 返回 BEFORE 的方法。"
    )
    if back_icon_coord is not None:
        context_text += (
            "\n\n[图像标注] AFTER 截图左上角已用红框标出 YOLO 检测到的图标，请结合截图判断其实际类型。"
        )
    if target_label:
        context_text += f"\n\n目标页面：BEFORE 是「{target_label}」，我们需要回到这个页面。"
    if nav_context:
        context_text += f"\n\n触发操作：{nav_context}"
    if after_elements is not None:
        elem_ctx = _format_elements_context(after_elements)
        if elem_ctx:
            context_text += f"\n\n{elem_ctx}"

    if failed_attempts:
        context_text += "\n\n之前的尝试均失败，请重新分析并给出不同的方案："
        for i, attempt in enumerate(failed_attempts, 1):
            context_text += f"\n- 第{i}次：坐标({attempt.get('x', '?')}, {attempt.get('y', '?')})，{attempt.get('reason', '未知原因')}"

    messages = [
        SystemMessage(content=BACK_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": context_text},
            {"type": "text", "text": "BEFORE（点击前）:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{before_b64}"}},
            {"type": "text", "text": "AFTER（点击后）:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{after_b64}"}},
        ]),
    ]
    action = invoke_structured(llm, messages, BackAction)
    print(f"    [LLM raw] can_go_back={action.can_go_back}  type={action.page_type}  "
          f"({action.back_x:.0f},{action.back_y:.0f})  {action.method}")

    result = action if (action.can_go_back and is_valid_tap(action.back_x, action.back_y)) else None
    if not action.can_go_back:
        pass
    elif not is_valid_tap(action.back_x, action.back_y):
        print(f"    [LLM] 坐标 ({action.back_x:.0f},{action.back_y:.0f}) 越界，无效")

    if debug_path is not None and debug_fn is not None:
        try:
            response_dict = (
                {"page_type": result.page_type, "can_go_back": result.can_go_back,
                 "method": result.method, "back_x": result.back_x, "back_y": result.back_y}
                if result is not None else None
            )
            debug_fn(debug_path, context_text, before_b64, after_b64, response_dict)
            print(f"    [LLM debug] {debug_path}")
        except Exception as exc:
            print(f"    [LLM debug] 保存失败: {exc}")

    return result


def _llm_log_entry(action: BackAction | None, reason: str = "") -> dict:
    """Build a log fragment capturing the raw LLM response."""
    if action is None:
        return {"llm_can_go_back": False, "llm_page_type": "", "llm_method": reason,
                "llm_x": -1, "llm_y": -1}
    return {"llm_can_go_back": action.can_go_back, "llm_page_type": action.page_type,
            "llm_method": action.method,
            "llm_x": round(action.back_x), "llm_y": round(action.back_y)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def back_shot_path(out_dir: Path | None, tap_index: int, attempt_num: int) -> Path | None:
    if out_dir is None:
        return None
    prefix = f"tap_{tap_index:02d}" if tap_index > 0 else "end"
    return out_dir / f"{prefix}_back_{attempt_num:02d}.png"


def save_if_changed(png_bytes: bytes | None, path: Path | None) -> str:
    """Save screenshot only when the screen actually changed. Returns path str or ''."""
    if path and png_bytes:
        path.write_bytes(png_bytes)
        return str(path)
    return ""


# ---------------------------------------------------------------------------
# Core navigation
# ---------------------------------------------------------------------------

def _match_stack(
    comp: PageComparator,
    stack: list[tuple[bytes, tuple[float, float] | None]],
    current_bytes: bytes,
) -> int:
    """Match current page against the nav stack. Returns stack index or -1."""
    for i, (page_bytes, _) in enumerate(stack):
        if comp.is_same_page(page_bytes, current_bytes).matched:
            return i
    return -1


def _navigate_forward(
    client,
    stack: list[tuple[bytes, tuple[float, float] | None]],
    from_level: int,
    screenshot: Callable[[], bytes],
    log: list[dict] | None = None,
    tap_index: int = 0,
    out_dir: Path | None = None,
) -> None:
    """Navigate forward from stack[from_level] to the top of stack (initial page)."""
    for i in range(from_level, len(stack) - 1):
        coords = stack[i][1]
        if coords is None:
            continue
        client.tap(*coords)
        time.sleep(BACK_SETTLE_SECONDS)
        after = screenshot()
        if log is not None:
            shot_str = ""
            if out_dir is not None and after:
                shot_path = back_shot_path(out_dir, tap_index, len(log) + 1)
                if shot_path:
                    shot_path.write_bytes(after)
                    shot_str = str(shot_path)
            log.append({"strategy": "forward", "coords": [round(coords[0]), round(coords[1])],
                        "result": f"L{i}→L{i+1}", "success": True, "screenshot": shot_str})


def _yolo_detect(png_bytes: bytes) -> tuple[float, float] | None:
    """YOLO detect nearest back/close icon. Returns normalized (ax, ay) or None."""
    from policy_expr.adapters.iphone.recon.yolo_calibrator import YoloCalibrator
    cal = YoloCalibrator.from_png(png_bytes)
    if cal is None:
        return None
    return cal.nearest(*BACK_TAP_CENTER, max_dist=BACK_TAP_MAX_DIST)


def _yolo_detect_near(png_bytes: bytes, ax: float, ay: float,
                      max_dist: float = 100.0) -> tuple[float, float] | None:
    """YOLO detect icon nearest to given coords. Returns normalized (ax, ay) or None."""
    from policy_expr.adapters.iphone.recon.yolo_calibrator import YoloCalibrator
    cal = YoloCalibrator.from_png(png_bytes)
    if cal is None:
        return None
    return cal.nearest(ax, ay, max_dist=max_dist)


def _annotate_back_icons(png_bytes: bytes) -> tuple[bytes, tuple[float, float] | None]:
    """Draw red bounding boxes on YOLO-detected icons in the top-left back-arrow zone.

    Returns (annotated_png, primary_coord) where primary_coord is the normalized
    (x, y) of the highest-confidence icon, or None if no icons found.
    Original bytes are returned unchanged when nothing is found.
    """
    try:
        import io as _io
        from PIL import Image, ImageDraw
        from policy_expr.adapters.iphone.recon.yolo_calibrator import YoloCalibrator

        cal = YoloCalibrator.from_png(png_bytes)
        if cal is None:
            return png_bytes, None

        # Restrict to the strict top-left zone where iOS back arrows live:
        # normalized x < 150, y < 200 (avoids top-right nav icons on main pages).
        nearby = [
            b for b in cal.boxes
            if (b.cx / cal.img_w * 1000) < 150 and (b.cy / cal.img_h * 1000) < 200
        ]
        if not nearby:
            return png_bytes, None

        # Pick the highest-confidence icon as the primary candidate.
        primary = max(nearby, key=lambda b: b.conf)
        primary_coord = (primary.cx / cal.img_w * 1000, primary.cy / cal.img_h * 1000)

        img = Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        for b in nearby:
            pad = 5
            draw.rectangle(
                [b.x1 - pad, b.y1 - pad, b.x2 + pad, b.y2 + pad],
                outline=(220, 30, 30), width=4,
            )

        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), primary_coord
    except Exception:
        return png_bytes, None


# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

STRATEGIES = ["fixed", "LLM_1", "LLM_2", "LLM_3"]


def _page_hash(png_bytes: bytes) -> str:
    """Short hash for log trace."""
    import hashlib
    return hashlib.md5(png_bytes).hexdigest()[:6]


def _next_strategy(tried: set[str]) -> str | None:
    """Given tried set, return next untried strategy. Pure function."""
    return next((s for s in STRATEGIES if s not in tried), None)


def _nav_print(msg: str, *, page_hash: str, round_num: int, strategy: str = ""):
    prefix = f"[nav:{page_hash}] R{round_num}"
    s = f" {strategy}" if strategy else ""
    print(f"    {prefix}{s} {msg}")


def _try_tap(
    client,
    screenshot: Callable[[], bytes],
    before_bytes: bytes | None,
    strategy: str,
    tap_fn: Callable[[], tuple | None],
    log: list[dict],
    save_path: Path | None = None,
    tap_xy: tuple[float, float] | None = None,
) -> tuple[float, float, bytes] | None:
    """Execute one tap, check if page changed.

    tap_fn: performs the tap, returns (lx, ly, response, ...) or None if can't tap.
    tap_xy: normalized (0-1000) coordinates for visualization.
    Returns (lx, ly, after_bytes) if page changed, None otherwise.
    """
    coords = tap_fn()
    if coords is None:
        return None

    lx, ly = coords[0], coords[1]
    base_entry = {"strategy": strategy, "coords": [round(lx), round(ly)]}
    if tap_xy is not None:
        base_entry["tap_xy"] = [round(tap_xy[0]), round(tap_xy[1])]

    tap_response = coords[2] if len(coords) > 2 else ""

    # Handle Mac system popup blocking tap (e.g. microphone access dialog)
    if "paused" in tap_response.lower():
        from policy_expr.adapters.iphone.perception import dismiss_iphone_sheet
        print(f"    ↩ [{strategy}] Mac 弹窗阻断，尝试恢复...")
        if dismiss_iphone_sheet():
            time.sleep(0.5)
            coords = tap_fn()
            if coords is None:
                return None
            lx, ly = coords[0], coords[1]
            tap_response = coords[2] if len(coords) > 2 else ""
            if tap_xy is not None:
                base_entry["tap_xy"] = [round(tap_xy[0]), round(tap_xy[1])]
            base_entry["coords"] = [round(lx), round(ly)]

    tap_failed = tap_response and ("failed" in tap_response.lower() or "interrupted" in tap_response.lower())
    if tap_failed:
        print(f"    ↩ [{strategy}] ({lx:.0f},{ly:.0f}) → tap 失败: {tap_response}")
        log.append({**base_entry, "result": f"tap 失败: {tap_response}",
                    "success": False, "screenshot": ""})
        return None

    time.sleep(BACK_SETTLE_SECONDS)
    after_bytes = screenshot()

    comp = _get_change_comp()
    if before_bytes and after_bytes:
        unchanged, score = comp.no_change_score(before_bytes, after_bytes)
        if unchanged:
            diff_ratio = _pixel_diff_ratio(before_bytes, after_bytes)
            if diff_ratio < _PIXEL_DIFF_THRESHOLD:
                print(f"    ↩ [{strategy}] ({lx:.0f},{ly:.0f}) → 未变化 (pixel_diff={diff_ratio:.4f})")
                log.append({**base_entry, "result": "未变化", "score": round(score, 3),
                            "success": False, "screenshot": ""})
                return None
            print(f"    ↩ [{strategy}] ({lx:.0f},{ly:.0f}) → 已变化 (pixel={diff_ratio:.4f}, edge={score:.3f})")
        else:
            # edge_iou reports changed — cascade confirms whether navigation actually occurred
            id_comp = _get_identity_comp()
            if id_comp.is_same_page(before_bytes, after_bytes).matched:
                print(f"    ↩ [{strategy}] ({lx:.0f},{ly:.0f}) → 动态内容误报（cascade同一页，视为未变化）")
                log.append({**base_entry, "result": "动态内容（未导航）", "score": round(score, 3),
                            "success": False, "screenshot": ""})
                return None

    shot_str = save_if_changed(after_bytes, save_path)
    print(f"    ↩ [{strategy}] ({lx:.0f},{ly:.0f}) → 已变化")
    log.append({**base_entry, "result": "已变化", "screenshot": shot_str})
    return lx, ly, after_bytes


def _execute_strategy(
    client,
    screenshot: Callable[[], bytes],
    before_bytes: bytes | None,
    initial_bytes: bytes,
    strategy: str,
    nav_context: str,
    out_dir: Path | None,
    tap_index: int,
    log: list[dict],
    llm_failed_attempts: list[dict] | None = None,
    *,
    page_hash: str = "",
    round_num: int = 0,
    target_label: str = "",
    after_elements: list[dict] | None = None,
    debug_fn: Callable | None = None,
) -> tuple[float, float, bytes] | None:
    """Execute one strategy with optional YOLO fallback.

    Strategies: "fixed", "LLM_1", "LLM_2", "LLM_3".
    Returns (lx, ly, after_bytes) if page changed, None otherwise.
    """
    from policy_expr.adapters.iphone.executor import logical_xy

    current_bytes = screenshot()

    def _tap_yolo_point(point: tuple[float, float]) -> tuple[float, float, str] | None:
        lx, ly = logical_xy(*point)
        resp = client.tap(lx, ly)
        return lx, ly, resp

    if strategy == "fixed":
        # Primary: fixed back tap
        save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
        result = _try_tap(client, screenshot, before_bytes, "fixed",
                          lambda: tap_back(client), log, save_path,
                          tap_xy=BACK_TAP_CENTER)
        if result is not None:
            return result

        # Fallback: YOLO near back button
        yolo_point = _yolo_detect(current_bytes)
        if yolo_point is not None:
            _nav_print(f"({yolo_point[0]:.0f},{yolo_point[1]:.0f}) YOLO回退",
                       page_hash=page_hash, round_num=round_num, strategy="fixed+YOLO")
            save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
            result = _try_tap(client, screenshot, before_bytes, "fixed+YOLO",
                              lambda: _tap_yolo_point(yolo_point), log, save_path,
                              tap_xy=yolo_point)
            if result is not None:
                return result
        else:
            log.append({"strategy": "fixed+YOLO", "result": "YOLO搜索范围内无图标",
                        "success": False, "screenshot": ""})
        return None

    # LLM strategies: "LLM_1", "LLM_2", "LLM_3"
    debug_path = (out_dir / f"R{round_num}_{strategy}_prompt.html") if out_dir else None
    llm_action = infer_back_action(initial_bytes, current_bytes, nav_context=nav_context,
                                    target_label=target_label,
                                    failed_attempts=llm_failed_attempts,
                                    after_elements=after_elements,
                                    debug_fn=debug_fn,
                                    debug_path=debug_path)
    if llm_action is None:
        log.append({"strategy": strategy, "result": "未能识别返回动作",
                    "success": False, "screenshot": "",
                    "llm_context": nav_context,
                    "llm_failed_attempts": list(llm_failed_attempts or []),
                    **_llm_log_entry(None, "can_go_back=False 或坐标越界")})
        return None

    # Validate: reject if LLM points at the nav_context tab (it's already selected in AFTER)
    if after_elements and nav_context:
        import re as _re3
        _tab_m2 = _re3.search(r"点击了tab「(.+?)」", nav_context)
        if _tab_m2:
            _forbidden = _tab_m2.group(1)
            _ax, _ay = llm_action.back_x, llm_action.back_y
            for _el in after_elements:
                if (_el.get("label") == _forbidden
                        and _el.get("element_type") == "tab"
                        and abs(_ax - _el.get("x", -999)) < 80
                        and abs(_ay - _el.get("y", -999)) < 80):
                    _reason = f"指向了触发跳转的 tab「{_forbidden}」（AFTER 中已选中），应点 BEFORE 中选中的 tab"
                    print(f"    [LLM] 坐标校验拒绝：{_reason}")
                    log.append({"strategy": strategy, "result": f"校验拒绝：{_reason}",
                                "success": False, "screenshot": "",
                                **_llm_log_entry(llm_action)})
                    llm_failed_attempts = list(llm_failed_attempts or [])
                    llm_failed_attempts.append({
                        "x": round(_ax), "y": round(_ay),
                        "reason": _reason, "method": llm_action.method,
                    })
                    return None

    # Primary: tap LLM coords
    llm_xy = (llm_action.back_x, llm_action.back_y)
    llm_result = tap_llm_back(client, llm_action)
    if llm_result is not None:
        save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
        result = _try_tap(client, screenshot, before_bytes, strategy,
                          lambda: llm_result, log, save_path,
                          tap_xy=llm_xy)
        log[-1].update(_llm_log_entry(llm_action))
        if result is not None:
            return result
    else:
        bx, by = llm_action.back_x, llm_action.back_y
        log.append({"strategy": strategy, "coords": [round(bx), round(by)],
                    "tap_xy": [round(bx), round(by)],
                    "result": "无效坐标", "success": False, "screenshot": "",
                    "llm_context": nav_context,
                    "llm_failed_attempts": list(llm_failed_attempts or []),
                    **_llm_log_entry(llm_action)})

    # Fallback: YOLO near LLM coords
    yolo_point = _yolo_detect_near(current_bytes, llm_action.back_x, llm_action.back_y)
    if yolo_point is not None:
        _nav_print(f"({yolo_point[0]:.0f},{yolo_point[1]:.0f}) YOLO校准 LLM({llm_action.back_x:.0f},{llm_action.back_y:.0f})",
                   page_hash=page_hash, round_num=round_num, strategy=f"{strategy}+YOLO")
        save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
        result = _try_tap(client, screenshot, before_bytes, f"{strategy}+YOLO",
                          lambda: _tap_yolo_point(yolo_point), log, save_path,
                          tap_xy=yolo_point)
        if result is not None:
            log[-1].update(_llm_log_entry(llm_action))
            return result
    else:
        log.append({"strategy": f"{strategy}+YOLO", "result": "LLM坐标附近无YOLO检测结果",
                    "success": False, "screenshot": "",
                    **_llm_log_entry(llm_action),
                    "llm_context": nav_context,
                    "llm_failed_attempts": list(llm_failed_attempts or [])})

    return None


def return_to_initial(
    client,
    screenshot: Callable[[], bytes],
    nav_stack: list[tuple[bytes, tuple[float, float] | None]],
    before_back_bytes: bytes | None = None,
    out_dir: Path | None = None,
    tap_index: int = 0,
    nav_context: str = "",
    target_label: str = "",
    after_elements: list[dict] | None = None,
    debug_fn: Callable | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> tuple[bool, list[dict]]:
    """Navigate back to the initial page (top of nav_stack).

    Single-loop architecture: each iteration picks the next untried strategy
    for the current page, executes it (with optional YOLO fallback), then
    assesses where we ended up via stack matching.

    page_records[(bytes, tried_set)] tracks per-page strategy history.
    """
    import re as _re

    log: list[dict] = []
    id_comp = _get_identity_comp()
    top_level = len(nav_stack) - 1
    initial_bytes = nav_stack[top_level][0]
    max_rounds = 12

    # For tab-switch navigation, skip the fixed back-button strategy: tapping
    # the left-corner button on a tab page corrupts AFTER before LLM can see
    # the original tab change, causing type C misclassification and loops.
    pre_tried: set[str] = {"fixed"} if _re.search(r"点击了tab「", nav_context) else set()

    # Per-page strategy history: list of (page_screenshot, tried_strategies).
    page_records: list[tuple[bytes, set[str]]] = []

    def _get_tried(current_bytes: bytes) -> set[str]:
        for page_bytes, tried in page_records:
            if id_comp.is_same_page(page_bytes, current_bytes).matched:
                return tried
        tried: set[str] = set(pre_tried)
        page_records.append((current_bytes, tried))
        return tried

    current_bytes = before_back_bytes or initial_bytes
    consecutive_unknown = 0
    llm_failed_attempts: list[dict] = []

    round_num = 0
    for round_num in range(1, max_rounds + 1):
        tried = _get_tried(current_bytes)
        ph = _page_hash(current_bytes)

        strategy = _next_strategy(tried)
        if strategy is None:
            _nav_print("当前页面所有策略均已尝试，放弃",
                       page_hash=ph, round_num=round_num)
            break

        _nav_print(f"尝试 {strategy}", page_hash=ph, round_num=round_num)
        _STRATEGY_LABELS = {"fixed": "固定返回", "LLM_1": "视觉回退①",
                            "LLM_2": "视觉回退②", "LLM_3": "视觉回退③"}
        if status_cb:
            status_cb(f"← 回退 R{round_num} {_STRATEGY_LABELS.get(strategy, strategy)}")

        # Save before screenshot for trace visualization
        if out_dir:
            before_path = out_dir / f"R{round_num}_before_{ph}.png"
            before_path.write_bytes(current_bytes)

        log_len_before = len(log)
        tap_result = _execute_strategy(
            client, screenshot, current_bytes, initial_bytes,
            strategy, nav_context, out_dir, tap_index, log,
            llm_failed_attempts=llm_failed_attempts,
            page_hash=ph, round_num=round_num,
            target_label=target_label,
            after_elements=after_elements if round_num == 1 else None,
            debug_fn=debug_fn,
        )
        tried.add(strategy)

        # Tag all new log entries from this round
        for e in log[log_len_before:]:
            e["page_from"] = ph
            e["round_num"] = round_num

        if tap_result is None:
            # Track failed LLM actions even when page didn't change
            if strategy.startswith("LLM"):
                last_log = next(
                    (e for e in reversed(log[log_len_before:])
                     if e.get("llm_x", -1) >= 0), {}
                )
                llm_failed_attempts.append({
                    "x": last_log.get("llm_x", -1),
                    "y": last_log.get("llm_y", -1),
                    "reason": "点击后页面未发生变化",
                    "method": last_log.get("llm_method", ""),
                })
            continue  # strategy failed, try next

        _, _, back_bytes = tap_result
        back_ph = _page_hash(back_bytes)

        # Assess where we ended up
        matched_level = _match_stack(id_comp, nav_stack, back_bytes)
        if matched_level >= 0:
            consecutive_unknown = 0
            is_initial = matched_level == top_level
            sim = id_comp.raw_similarity(nav_stack[matched_level][0], back_bytes)
            level_desc = "initial" if is_initial else f"L{matched_level}"
            _nav_print(f"→ matched {level_desc} ({sim:.3f}) {'✓' if is_initial else ''}",
                       page_hash=ph, round_num=round_num)
            if status_cb:
                status_cb(f"← 回退 R{round_num} ✓ {'回到初始页' if is_initial else f'→ {level_desc}'}")
            if log:
                log[-1]["result"] = level_desc
                log[-1]["score"] = round(sim, 3)
                log[-1]["success"] = is_initial
                log[-1]["page_to"] = back_ph
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        # Check if this is a previously visited page
        is_known = any(
            id_comp.is_same_page(pb, back_bytes).matched
            for pb, _ in page_records
        )

        if is_known:
            page_label = "已访问页"
            consecutive_unknown = 0
        else:
            consecutive_unknown += 1
            page_label = f"未知页#{consecutive_unknown}"

        sim_to_initial = id_comp.raw_similarity(initial_bytes, back_bytes)
        max_sim = 0.0
        for page_bytes, _ in nav_stack:
            sim = id_comp.raw_similarity(page_bytes, back_bytes)
            max_sim = max(max_sim, sim)

        _nav_print(f"→ {page_label} (initial: {sim_to_initial:.3f}, stack_max: {max_sim:.3f})",
                   page_hash=ph, round_num=round_num)
        if status_cb:
            status_cb(f"← 回退 R{round_num} → {page_label}")
        if log:
            log[-1]["result"] = f"{page_label} (initial: {sim_to_initial:.3f}, stack_max: {max_sim:.3f})"
            log[-1]["score"] = round(sim_to_initial, 3)
            log[-1]["page_to"] = back_ph

        if not is_known and consecutive_unknown >= 5:
            _nav_print("连续5次落入全新未知页面，放弃",
                       page_hash=ph, round_num=round_num)
            break

        # Track failed LLM actions for cycle avoidance
        last_log = log[-1] if log else {}
        last_strategy = last_log.get("strategy", "")
        if last_strategy.startswith("LLM"):
            llm_failed_attempts.append({
                "x": last_log.get("llm_x", -1),  # normalized 0-1000, matches LLM coord space
                "y": last_log.get("llm_y", -1),
                "reason": f"跳转到了{page_label}，未回到目标页面",
                "method": last_log.get("llm_method", ""),
            })

        current_bytes = back_bytes

    _nav_print("所有回退策略均未成功返回初始页面",
               page_hash=_page_hash(current_bytes), round_num=round_num)
    if status_cb:
        status_cb(f"← 回退失败 (R{round_num})")
    return False, log


def manual_recover(
    client,
    screenshot,
    nav_stack: list[tuple[bytes, tuple[float, float] | None]],
    top_level: int,
    prompt: str = "",
    max_attempts: int = 3,
) -> bool:
    """Prompt user to manually navigate back to initial page.

    Saves the target page screenshot to /tmp for user reference.
    Returns True if the user successfully recovered, False if aborted.
    """
    import tempfile
    comp = _get_identity_comp()
    initial_bytes = nav_stack[top_level][0]

    ref_path = Path(tempfile.gettempdir()) / "recon_target_page.png"
    ref_path.write_bytes(initial_bytes)

    for attempt in range(1, max_attempts + 1):
        print(f"\n  ⚠ {prompt}")
        print(f"  目标截图: {ref_path}")
        print(f"  请手动操作手机回到目标页面，然后按回车继续 ({attempt}/{max_attempts})")
        print(f"  （输入 q 放弃当前页面的探测）", end="", flush=True)
        user_input = input()

        if user_input.strip().lower() == "q":
            print("  用户放弃，终止探测")
            return False

        current_bytes = screenshot()
        sim = comp.raw_similarity(initial_bytes, current_bytes)
        print(f"  当前截图与初始页相似度: {sim:.3f}")

        if sim >= comp._no_change_threshold:
            print(f"  ✓ 已回到初始页")
            return True

        matched_level = _match_stack(comp, nav_stack, current_bytes)
        if matched_level >= 0:
            if matched_level == top_level:
                print(f"  ✓ 已回到初始页")
                return True
            print(f"  匹配到祖先页 L{matched_level}，尝试自动 forward 回初始页...")
            _navigate_forward(client, nav_stack, matched_level, screenshot)
            verify_bytes = screenshot()
            verify_sim = comp.raw_similarity(initial_bytes, verify_bytes)
            if verify_sim >= comp._no_change_threshold:
                print(f"  ✓ forward 成功，已回到初始页 ({verify_sim:.3f})")
                return True
            print(f"  forward 后仍未到达初始页 ({verify_sim:.3f})")

    print("  多次尝试未能回到初始页，终止探测")
    return False
