"""Direct Action Policy back navigation (no Planner).

Architecture per round:
  1. Fast identity check (no LLM) — return if already at target or ancestor page
  2. Construct instruction (tab switch or exit prompt, no LLM)
  3. Action Policy (1 LLM call) — sees current screenshot + instruction → ActionDecision with coords
  4. Execute (tap/scroll/home) + YOLO calibration
  5. Verify page change → update state → loop
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path

from policy_expr.adapters.iphone.executor import is_valid_tap, logical_xy
from policy_expr.policies.structured_output import StructuredOutputPolicy
from policy_expr.adapters.iphone.recon.back_nav import (
    BACK_SETTLE_SECONDS,
    _PIXEL_DIFF_THRESHOLD,
    _get_identity_comp,
    _match_stack,
    _navigate_forward,
    _pixel_diff_ratio,
    _yolo_detect_near,
    back_shot_path,
    save_if_changed,
)
from policy_expr.schemas import Observation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ROUNDS = 10
_MAX_CONSECUTIVE_NO_CHANGE = 3
_MAX_PAGE_VISITS = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_hash(png_bytes: bytes) -> str:
    return hashlib.md5(png_bytes).hexdigest()[:6]


def _nav_print(msg: str, *, page_hash: str, round_num: int):
    print(f"    [nav2:{page_hash}] R{round_num} {msg}")


# ---------------------------------------------------------------------------
# Instruction builder (exported for eval reuse)
# ---------------------------------------------------------------------------

_EXIT_INSTRUCTION = (
    "当前页面需要退出到上一层。找到页面中的退出元素并点击："
    "左上角返回箭头（< 或 ←）、右上角或左上角的关闭/取消按钮（×/取消/关闭/Cancel）、"
    "确认弹窗中的离开选项（不保存/放弃/离开/Discard/Leave）、"
    "或底部操作栏的返回/完成按钮。"
)


def build_back_instruction(nav_context: str, selected_tab: str) -> str:
    """Construct the Action Policy instruction for back navigation.

    Tab cases use a targeted tap instruction; all other cases use the
    generic exit prompt.
    """
    if "tab" in nav_context and selected_tab:
        position = ""
        if "底部tab" in nav_context:
            position = "底部"
        elif "顶部tab" in nav_context:
            position = "顶部"
        return f"点击{position}tab栏中的「{selected_tab}」"
    return _EXIT_INSTRUCTION


# ---------------------------------------------------------------------------
# Tap coordinate cache
# ---------------------------------------------------------------------------

# In-process cache: (instruction, structural_hash) → (ax, ay) pre-YOLO
# Avoids repeated LLM calls when returning to structurally identical pages.
_tap_cache: dict[str, tuple[float, float]] = {}


def _structural_hash(png_bytes: bytes) -> str:
    """Hash the top + bottom navigation bands — stable across dynamic feed content."""
    import io
    from PIL import Image as _Image
    img = _Image.open(io.BytesIO(png_bytes)).convert("L")
    w, h = img.size
    band = max(1, h // 8)
    top = img.crop((0, 0, w, band)).resize((32, 4))
    bot = img.crop((0, h - band, w, h)).resize((32, 4))
    return hashlib.md5(top.tobytes() + bot.tobytes()).hexdigest()[:8]


def _cache_key(instruction: str, png_bytes: bytes) -> str:
    return f"{instruction[:40]}|{_structural_hash(png_bytes)}"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def planned_return_to_initial(
    client,
    screenshot: Callable[[], bytes],
    nav_stack: list[tuple[bytes, tuple[float, float] | None]],
    before_back_bytes: bytes | None = None,
    out_dir: Path | None = None,
    tap_index: int = 0,
    nav_context: str = "",
    status_cb: Callable[[str], None] | None = None,
    selected_tab: str = "",
) -> tuple[bool, list[dict]]:
    """Navigate back to the initial page using direct Action Policy (no Planner).

    Drop-in replacement for back_nav.return_to_initial().
    """
    log: list[dict] = []
    id_comp = _get_identity_comp()
    top_level = len(nav_stack) - 1
    action_policy = StructuredOutputPolicy()
    current_bytes = before_back_bytes or screenshot()

    # 给 nav_context 补充 top/bottom 位置信息
    # tap_coords are stored in the PARENT entry (nav_stack[top_level - 1][1]),
    # not in the current page entry (nav_stack[top_level][1]) which is always None.
    annotated_context = nav_context
    if nav_context and "tab" in nav_context:
        tap_coords = nav_stack[top_level - 1][1] if top_level > 0 else None
        if tap_coords:
            _, tap_y = tap_coords
            if tap_y > 800:
                annotated_context = nav_context.replace("tab", "底部tab", 1)
            elif tap_y < 350:
                annotated_context = nav_context.replace("tab", "顶部tab", 1)

    consecutive_no_change = 0
    consecutive_no_action = 0
    page_visit_counts: dict[str, int] = {}

    round_num = 0
    for round_num in range(1, _MAX_ROUNDS + 1):
        ph = _page_hash(current_bytes)
        _nav_print("开始", page_hash=ph, round_num=round_num)
        if status_cb:
            status_cb(f"← 回退 R{round_num}")

        if out_dir:
            (out_dir / f"R{round_num}_before_{ph}.png").write_bytes(current_bytes)

        # ── Step 1: Fast identity check (no LLM) ──
        matched_level = _match_stack(id_comp, nav_stack, current_bytes)
        if matched_level >= 0:
            is_initial = matched_level == top_level
            sim = id_comp.identity_similarity(nav_stack[matched_level][0], current_bytes)
            level_desc = "initial" if is_initial else f"L{matched_level}"
            _nav_print(f"已匹配 {level_desc} ({sim:.3f})", page_hash=ph, round_num=round_num)
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        # ── Step 2: Construct instruction ──
        instruction = build_back_instruction(annotated_context, selected_tab)

        _nav_print(f"指令: {instruction}", page_hash=ph, round_num=round_num)

        # ── Step 3: resolve tap coords (cache → LLM) ──
        cache_key = _cache_key(instruction, current_bytes)
        cached = _tap_cache.get(cache_key)
        _raw_tap: tuple[float, float] | None = None  # pre-YOLO coords (cache or LLM)
        tap_xy = None
        result: str = ""
        strategy = "cached" if cached is not None else "direct"

        if cached is not None:
            _nav_print(f"[cache] 命中 ({cached[0]:.0f},{cached[1]:.0f})，跳过 LLM", page_hash=ph, round_num=round_num)
            _raw_tap = cached
        else:
            obs = Observation(png_bytes=current_bytes, source="planned_back_nav")
            decision = action_policy.decide(obs, instruction)

            if decision.not_found_reason:
                _nav_print(f"目标不可见: {decision.not_found_reason}", page_hash=ph, round_num=round_num)
                log.append({"round": round_num, "strategy": strategy,
                            "instruction": instruction,
                            "result": f"元素不可见: {decision.not_found_reason}",
                            "success": False})
                continue

            action = decision.action

            if action.action_type == "home":
                result = client.press_home()
                if "Failed" in result:
                    from policy_expr.adapters.iphone.executor import WIN_W, WIN_H
                    result = client.tap(WIN_W / 2, WIN_H - 16)
            elif action.action_type == "scroll" and action.direction:
                _execute_scroll(action)
                tap_xy = (action.x or 500, action.y or 500)
                result = "scroll"
            elif action.action_type in ("tap", "click") and action.x is not None and action.y is not None:
                _raw_tap = (action.x, action.y)
            else:
                consecutive_no_action += 1
                reason = (f"tap 坐标为空" if action.action_type in ("tap", "click")
                          else f"不支持的动作: {action.action_type}")
                _nav_print(f"无法执行: {reason} (连续 {consecutive_no_action} 次)",
                           page_hash=ph, round_num=round_num)
                log.append({"round": round_num, "strategy": strategy,
                            "instruction": instruction,
                            "result": reason, "success": False})
                if consecutive_no_action >= _MAX_CONSECUTIVE_NO_CHANGE:
                    _nav_print("连续无法执行，放弃", page_hash=ph, round_num=round_num)
                    break
                continue

        consecutive_no_action = 0

        # ── Step 4: Execute tap (shared for cache hit and LLM tap) ──
        if _raw_tap is not None:
            ax, ay = _raw_tap
            yolo_point = _yolo_detect_near(current_bytes, ax, ay)
            if yolo_point:
                _nav_print(f"YOLO校准: ({ax:.0f},{ay:.0f})→({yolo_point[0]:.0f},{yolo_point[1]:.0f})",
                           page_hash=ph, round_num=round_num)
                ax, ay = yolo_point
            if not is_valid_tap(ax, ay):
                if cached is not None:
                    _tap_cache.pop(cache_key, None)
                log.append({"round": round_num, "strategy": strategy,
                            "instruction": instruction, "result": "坐标越界", "success": False})
                continue
            lx, ly = logical_xy(ax, ay)
            tap_xy = (ax, ay)
            _nav_print(f"执行点击 ({lx:.0f},{ly:.0f})", page_hash=ph, round_num=round_num)
            result = client.tap(lx, ly)

        if isinstance(result, str) and ("failed" in result.lower() or "interrupted" in result.lower()):
            log.append({"round": round_num, "strategy": strategy,
                        "instruction": instruction,
                        "result": f"执行失败: {result}", "success": False})
            continue

        # ── Step 5: Verify ──
        time.sleep(BACK_SETTLE_SECONDS)
        after_bytes = screenshot()
        after_ph = _page_hash(after_bytes)

        matched_level = _match_stack(id_comp, nav_stack, after_bytes)
        if matched_level >= 0:
            is_initial = matched_level == top_level
            sim = id_comp.identity_similarity(nav_stack[matched_level][0], after_bytes)
            level_desc = "initial" if is_initial else f"L{matched_level}"
            _nav_print(f"→ 匹配 {level_desc} ({sim:.3f})", page_hash=ph, round_num=round_num)
            if status_cb:
                status_cb(f"← 回退 R{round_num} ✓ {'回到初始页' if is_initial else level_desc}")
            if cached is None and _raw_tap is not None:
                _tap_cache[cache_key] = _raw_tap
                _nav_print(f"[cache] 已存储 ({_raw_tap[0]:.0f},{_raw_tap[1]:.0f})，下次回退此页跳过 LLM", page_hash=ph, round_num=round_num)
            save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
            shot_str = save_if_changed(after_bytes, save_path)
            log.append({
                "round": round_num, "strategy": strategy,
                "instruction": instruction,
                "result": level_desc, "score": round(sim, 3), "success": is_initial,
                "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        diff_ratio = _pixel_diff_ratio(current_bytes, after_bytes)
        if diff_ratio < _PIXEL_DIFF_THRESHOLD:
            consecutive_no_change += 1
            if cached is not None:
                _tap_cache.pop(cache_key, None)
                _nav_print(f"[cache] 失效（点击后页面无变化），下轮改用 LLM", page_hash=ph, round_num=round_num)
            _nav_print(f"未变化 (pixel={diff_ratio:.4f})", page_hash=ph, round_num=round_num)
            log.append({
                "round": round_num, "strategy": strategy,
                "instruction": instruction,
                "result": "未变化", "success": False,
                "page_from": ph, "page_to": after_ph,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            if consecutive_no_change >= _MAX_CONSECUTIVE_NO_CHANGE:
                _nav_print("连续无变化，放弃", page_hash=ph, round_num=round_num)
                break
            continue

        consecutive_no_change = 0
        page_visit_counts[after_ph] = page_visit_counts.get(after_ph, 0) + 1
        if page_visit_counts[after_ph] >= _MAX_PAGE_VISITS:
            _nav_print(f"页面循环 ({after_ph})，放弃", page_hash=ph, round_num=round_num)
            log.append({
                "round": round_num, "strategy": strategy,
                "instruction": instruction,
                "result": f"页面循环 ({after_ph})", "success": False,
                "page_from": ph, "page_to": after_ph,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            break

        log.append({
            "round": round_num, "strategy": strategy,
            "instruction": instruction,
            "result": "变化", "success": False,
            "page_from": ph, "page_to": after_ph,
            **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
        })
        current_bytes = after_bytes

    _nav_print("回退失败", page_hash=_page_hash(current_bytes), round_num=round_num)
    if status_cb:
        status_cb(f"← 回退失败 (R{round_num})")
    return False, log


# ---------------------------------------------------------------------------
# Scroll helper
# ---------------------------------------------------------------------------

def _execute_scroll(action) -> None:
    """Execute a scroll action via Quartz events."""
    import subprocess
    from policy_expr.adapters.iphone.executor import (
        WIN_W, WIN_H, SCROLL_TICKS, SCROLL_DELTA, SCROLL_INTERVAL,
        _find_iphone_window, _quartz_hover,
    )
    from Quartz import (
        CGEventCreateScrollWheelEvent,
        CGEventSetLocation,
        CGEventPost,
        kCGScrollEventUnitPixel,
        kCGHIDEventTap,
    )

    origin = _find_iphone_window()
    if origin is None:
        print("  iPhone Mirroring 窗口未找到，无法滚轮")
        return
    wx, wy = origin

    subprocess.run(["osascript", "-e", 'tell application "iPhone Mirroring" to activate'],
                   capture_output=True)
    time.sleep(0.3)

    ax = action.x if action.x is not None else 500
    ay = action.y if action.y is not None else 500
    sx = wx + ax / 1000 * WIN_W
    sy = wy + ay / 1000 * WIN_H

    direction = (action.direction or "").strip().lower()
    if direction in ("up", "向上", "upward"):
        delta = SCROLL_DELTA
    elif direction in ("down", "向下", "downward"):
        delta = -SCROLL_DELTA
    elif direction in ("left", "向左", "leftward"):
        delta = -SCROLL_DELTA
    elif direction in ("right", "向右", "rightward"):
        delta = SCROLL_DELTA
    else:
        return

    _quartz_hover(sx, sy)
    time.sleep(0.1)

    for i in range(SCROLL_TICKS):
        ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitPixel, 1, delta)
        CGEventSetLocation(ev, (sx, sy))
        CGEventPost(kCGHIDEventTap, ev)
        if i < SCROLL_TICKS - 1:
            time.sleep(SCROLL_INTERVAL)
