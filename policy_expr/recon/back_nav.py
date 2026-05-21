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
from policy_expr.config import resolve_llm_config
from policy_expr.executor import is_valid_tap
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.recon.page_compare import PageComparator, make_comparator
from policy_expr.recon.utils import save_llm_prompt_debug


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
_change_comp: PageComparator = make_comparator("edge_iou")
_identity_comp: PageComparator = make_comparator("cascade")


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
    page_type: str = Field(description="导航类型：A=弹窗/浮层, B=底部tab切换, C=普通页面跳转")
    method: str = Field(description="返回方法描述")
    back_x: float = Field(default=-1, description="返回目标归一化 x 坐标（0-1000）")
    back_y: float = Field(default=-1, description="返回目标归一化 y 坐标（0-1000）")


BACK_PROMPT = """\
你是一个 iPhone 页面导航专家。用户给出了两张截图和触发操作描述，请分析如何从 AFTER 页面返回 BEFORE 页面。

## 坐标系
左上角 (0, 0)，右下角 (1000, 1000)。坐标是点击目标的视觉中心。
重要：返回按钮通常在 x=50-120, y=80-160 的范围内。不要输出 x<30 或 y<50 的坐标。

## 输出字段说明
请按以下顺序填写输出字段：
1. **page_type**（先填）：根据下方规则判断页面类型，填 "A"、"B" 或 "C"。这一步决定后续的返回策略。
2. **can_go_back**：是否能找到返回方法。
3. **method**：用一句话描述具体的返回操作。
4. **back_x / back_y**：目标元素中心的归一化坐标（0-1000）。

## 分析步骤
1. **先读「触发操作」**：它描述了用户点击了什么元素（含类型）才从 BEFORE 跳到 AFTER。
   - 触发操作含「底部tab」→ 直接判断类型 B，无需继续比对截图
   - 触发操作含「button / link / icon / back_button」→ 跳过 B，进入 A/C 的视觉判断
2. 若触发操作信息不足，再观察截图：按 A → B → C 顺序检查，命中即停止
3. 根据判定的类型，在 AFTER 截图上找到对应的返回元素
4. 输出该元素中心的坐标

## AFTER 页面类型定义

### 类型 A：弹窗/浮层
**判断依据**：AFTER 上有弹窗、对话框、底部弹出面板、广告浮层，覆盖在底层页面上方（底层页面仍可见）
→ 点击关闭/取消/跳过按钮（通常是 × 或「关闭」）

### 类型 B：底部 tab 切换
**判断依据**：触发操作中含「底部tab」；或 BEFORE 与 AFTER 有相同的底部导航栏且选中项不同
→ 点击 AFTER 中与 BEFORE 选中 tab 对应的那个底部 tab（即回到 BEFORE 时高亮的那个）
注意：不要点击 AFTER 中已经处于选中状态的 tab，那不会有任何效果

### 类型 C：普通页面跳转
**判断依据**：AFTER 是全新页面，左上角有返回箭头（‹）或关闭按钮（×），且触发操作不含「底部tab」
→ 点击左上角返回按钮

"""


# ---------------------------------------------------------------------------
# Tap operations
# ---------------------------------------------------------------------------

def tap_back(client) -> tuple[float, float, str]:
    """Tap the iOS back button area."""
    from policy_expr.executor import logical_xy

    lx, ly = logical_xy(*BACK_TAP_CENTER)
    result = client.tap(lx, ly)
    return lx, ly, result


def tap_llm_back(client, action: BackAction) -> tuple[float, float, str] | None:
    """Tap the return target selected by the vision model. Returns None if coords invalid."""
    from policy_expr.executor import logical_xy

    if not is_valid_tap(action.back_x, action.back_y):
        return None
    lx, ly = logical_xy(action.back_x, action.back_y)
    result = client.tap(lx, ly)
    return lx, ly, result



# ---------------------------------------------------------------------------
# LLM back action inference
# ---------------------------------------------------------------------------

def _parse_page_elements(png_bytes: bytes) -> list[dict]:
    """Run PageParser on a screenshot; returns serializable element list."""
    try:
        from policy_expr.recon.page_parser import PageParser
        parsed = PageParser().parse_screen(png_bytes)
        return [
            {
                "label": el.label,
                "element_type": el.element_type,
                "x": round(el.x),
                "y": round(el.y),
                "leads_to": el.leads_to,
            }
            for el in parsed.interactive_elements
        ]
    except Exception as exc:
        print(f"    [page_parser] 解析失败: {exc}")
        return []


def _format_elements_context(elements: list[dict]) -> str:
    """Format element list for inclusion in the LLM back-nav prompt."""
    if not elements:
        return ""
    lines = ["以下是AFTER页面检测到的可交互元素（请仅从这些元素中选择点击目标）："]
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
                      debug_path: Path | None = None) -> BackAction | None:
    """Ask the vision model how to navigate from AFTER back to BEFORE.

    Args:
        nav_context: How the user navigated from BEFORE to AFTER
                     (e.g. "点击了底部「发现」tab", "点击了「珠珠」聊天项").
        after_elements: Pre-parsed interactive elements on the AFTER page.
                        When provided, appended to prompt to ground LLM output.
        debug_path: When set, save an HTML visualization of the prompt + response here.
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
    after_b64 = base64.b64encode(resize_to_logical_png(after_png)).decode()

    context_text = (
        "第一张(BEFORE)是点击前的页面，第二张(AFTER)是点击后跳转的页面。"
        "请找出从 AFTER 返回 BEFORE 的方法。"
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

    if debug_path is not None:
        try:
            response_dict = (
                {"page_type": result.page_type, "can_go_back": result.can_go_back,
                 "method": result.method, "back_x": result.back_x, "back_y": result.back_y}
                if result is not None else None
            )
            save_llm_prompt_debug(debug_path, BACK_PROMPT, context_text, before_b64, after_b64, response_dict)
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
    from policy_expr.recon.yolo_calibrator import YoloCalibrator
    cal = YoloCalibrator.from_png(png_bytes)
    if cal is None:
        return None
    return cal.nearest(*BACK_TAP_CENTER, max_dist=BACK_TAP_MAX_DIST)


def _yolo_detect_near(png_bytes: bytes, ax: float, ay: float,
                      max_dist: float = 100.0) -> tuple[float, float] | None:
    """YOLO detect icon nearest to given coords. Returns normalized (ax, ay) or None."""
    from policy_expr.recon.yolo_calibrator import YoloCalibrator
    cal = YoloCalibrator.from_png(png_bytes)
    if cal is None:
        return None
    return cal.nearest(ax, ay, max_dist=max_dist)


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
        from policy_expr.perception import try_resume_mac
        print(f"    ↩ [{strategy}] Mac 弹窗阻断，尝试恢复...")
        if try_resume_mac():
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
) -> tuple[float, float, bytes] | None:
    """Execute one strategy with optional YOLO fallback.

    Strategies: "fixed", "LLM_1", "LLM_2", "LLM_3".
    Returns (lx, ly, after_bytes) if page changed, None otherwise.
    """
    from policy_expr.executor import logical_xy

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
                                    debug_path=debug_path)
    if llm_action is None:
        log.append({"strategy": strategy, "result": "未能识别返回动作",
                    "success": False, "screenshot": "",
                    "llm_context": nav_context,
                    "llm_failed_attempts": list(llm_failed_attempts or []),
                    **_llm_log_entry(None, "can_go_back=False 或坐标越界")})
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
) -> tuple[bool, list[dict]]:
    """Navigate back to the initial page (top of nav_stack).

    Single-loop architecture: each iteration picks the next untried strategy
    for the current page, executes it (with optional YOLO fallback), then
    assesses where we ended up via stack matching.

    page_records[(bytes, tried_set)] tracks per-page strategy history.
    """
    log: list[dict] = []
    id_comp = _get_identity_comp()
    top_level = len(nav_stack) - 1
    initial_bytes = nav_stack[top_level][0]
    max_rounds = 12

    # Per-page strategy history: list of (page_screenshot, tried_strategies).
    page_records: list[tuple[bytes, set[str]]] = []

    def _get_tried(current_bytes: bytes) -> set[str]:
        for page_bytes, tried in page_records:
            if id_comp.is_same_page(page_bytes, current_bytes).matched:
                return tried
        tried: set[str] = set()
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
