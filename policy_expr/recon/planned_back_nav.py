"""Planner-based back navigation: two-stage decision pipeline.

Architecture per round:
  1. Fast identity check (no LLM) — return if already at target or ancestor page
  2. Planner (1 LLM call) — sees current + target screenshots + history → instruction string
  3. Action Policy (1 LLM call) — sees current screenshot + instruction → ActionDecision with coords
  4. Execute (tap/scroll/home) + YOLO calibration
  5. Verify page change → update state → loop
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.executor import is_valid_tap, logical_xy
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.policies.structured_output import StructuredOutputPolicy
from policy_expr.recon.back_nav import (
    BACK_SETTLE_SECONDS,
    _PIXEL_DIFF_THRESHOLD,
    _get_change_comp,
    _get_identity_comp,
    _match_stack,
    _navigate_forward,
    _pixel_diff_ratio,
    _yolo_detect_near,
    back_shot_path,
    make_nav_context,
    save_if_changed,
)
from policy_expr.schemas import ActionDecision, Observation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ROUNDS = 10
_MAX_CONSECUTIVE_NO_CHANGE = 3
_REPEAT_SIMILARITY_THRESHOLD = 0.6
_MAX_PAGE_VISITS = 3


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class _BackPlanResult(BaseModel):
    instruction: str = Field(description="下一步操作指令，描述要操作的 UI 元素，不包含坐标")
    rationale: str = Field(description="选择该操作的判断依据")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

BACK_PLANNER_PROMPT = """\
你是 iPhone 应用返回导航规划器。根据两张截图（TARGET 和 CURRENT）以及触发操作描述，判断如何从 CURRENT 回回 TARGET。

## 判断流程（按顺序执行，满足即停）

### Q1：CURRENT 有浮层覆盖吗？
检查 CURRENT 全屏：是否有弹窗、对话框、底部弹出面板或广告浮层叠在底层页面上方？

浮层的识别依据：底层页面内容仍部分可见；浮层有独立边框/阴影/遮罩；有 ×/关闭/跳过/取消 控件。
⚠️ 小面积悬浮按钮（如浮动购物车图标、小浮窗广告）不算浮层，只有大面积遮盖主要页面内容的才算。

**→ 是：指令 = "点击弹窗的关闭按钮"**
→ 否：进入 Q2。

### Q2：某一排 tab 栏的选中项发生了变化吗？
iPhone 应用通常同时存在两类 tab 栏，须分开判断：
- **底部导航 tab**：3-5 个固定入口（首页/消息/我/购物车等），在不同页面间通常保持不变，位于页面最底部
- **顶部分类 tab**：内容分类标签（推荐/热门/关注等），随内容区切换而变化，位于页面上方

逐排对比 TARGET 和 CURRENT，对每一排检查以下三点是否**同时**成立：
1. 该排在两图中均存在
2. 该排的高亮/选中项在两图中**确实不同**（两图相同则跳过该排）
3. 触发操作的名称能在该排选项里找到

**→ 某一排同时满足三点：指令 = "点击[顶部/底部] tab 栏中的「TARGET 中选中的 tab 名称」"**
→ 所有排均不满足：进入 Q3。

注意：
- 不要点击 CURRENT 中已处于选中状态的 tab，要点击 TARGET 中选中的那个
- 触发操作含「tab」不代表条件 3 自动满足，须逐字核实 tab 名称确实出现在该排选项里

### Q3：点左上角返回按钮
**→ 指令 = "点击左上角返回箭头"**

## 规则
- 每步只输出一个原子操作
- 只描述要操作的 UI 元素，不要输出坐标
- 不重复之前失败的操作
"""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("back_nav")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url, temperature=0)


def _invoke_planner(
    target_png: bytes,
    current_png: bytes,
    nav_context: str = "",
    history: list[dict] | None = None,
    target_label: str = "",
) -> _BackPlanResult:
    """Ask the planner for the next navigation instruction."""
    llm = _make_llm()

    target_b64 = base64.b64encode(resize_to_logical_png(target_png)).decode()
    current_b64 = base64.b64encode(resize_to_logical_png(current_png)).decode()

    context_text = (
        "第一张(TARGET)是要回到的目标页面，第二张(CURRENT)是当前所在页面。"
        "请给出从 CURRENT 回到 TARGET 的下一步操作指令。"
    )
    if target_label:
        context_text += f"\n\n目标页面名称：「{target_label}」"
    if nav_context:
        context_text += f"\n\n触发操作：{nav_context}"
    if history:
        context_text += "\n\n之前的尝试："
        for i, h in enumerate(history[-6:], 1):
            inst = h.get("instruction", "?")
            result = h.get("result", "?")
            context_text += f"\n- 第{i}次：指令「{inst}」→ {result}"

    messages = [
        SystemMessage(content=BACK_PLANNER_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": context_text},
            {"type": "text", "text": "TARGET（目标页面）:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{target_b64}"}},
            {"type": "text", "text": "CURRENT（当前页面）:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{current_b64}"}},
        ]),
    ]
    return invoke_structured(llm, messages, _BackPlanResult)


# ---------------------------------------------------------------------------
# Stuck detection helpers
# ---------------------------------------------------------------------------

def _is_repeated_instruction(instruction: str, history: list[dict]) -> bool:
    """Check if instruction repeats a previously failed one."""
    failed = [h for h in history if "失败" in h.get("result", "") or "未变化" in h.get("result", "")]
    if not failed:
        return False
    clean_new = re.sub(r"[，。、；：""''《》\s（）\(\)]", "", instruction.strip())
    for h in failed:
        old = h.get("instruction", "")
        clean_old = re.sub(r"[，。、；：""''《》\s（）\(\)]", "", old.strip())
        if clean_new and clean_old:
            ratio = SequenceMatcher(None, clean_new, clean_old).ratio()
            if ratio >= _REPEAT_SIMILARITY_THRESHOLD:
                return True
    return False


def _page_hash(png_bytes: bytes) -> str:
    return hashlib.md5(png_bytes).hexdigest()[:6]


def _nav_print(msg: str, *, page_hash: str, round_num: int):
    print(f"    [nav2:{page_hash}] R{round_num} {msg}")


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
    target_label: str = "",
    after_elements: list[dict] | None = None,
    debug_fn: Callable | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> tuple[bool, list[dict]]:
    """Navigate back to the initial page using a planner-based loop.

    Drop-in replacement for back_nav.return_to_initial().
    """
    log: list[dict] = []
    id_comp = _get_identity_comp()
    top_level = len(nav_stack) - 1
    initial_bytes = nav_stack[top_level][0]

    action_policy = StructuredOutputPolicy()
    current_bytes = before_back_bytes or initial_bytes

    # Page visit counts for loop detection
    page_visit_counts: dict[str, int] = {}
    history: list[dict] = []
    consecutive_no_change = 0

    for round_num in range(1, _MAX_ROUNDS + 1):
        ph = _page_hash(current_bytes)
        _nav_print(f"开始规划", page_hash=ph, round_num=round_num)
        if status_cb:
            status_cb(f"← 回退 R{round_num} 规划中")

        # Save before screenshot for trace
        if out_dir:
            before_path = out_dir / f"R{round_num}_before_{ph}.png"
            before_path.write_bytes(current_bytes)

        # ── Step 1: Fast identity check (no LLM) ──
        matched_level = _match_stack(id_comp, nav_stack, current_bytes)
        if matched_level >= 0:
            is_initial = matched_level == top_level
            level_desc = "initial" if is_initial else f"L{matched_level}"
            sim = id_comp.raw_similarity(nav_stack[matched_level][0], current_bytes)
            _nav_print(f"已匹配 {level_desc} ({sim:.3f})", page_hash=ph, round_num=round_num)
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        # ── Step 2: Planner (1 LLM call) ──
        plan = _invoke_planner(
            initial_bytes, current_bytes,
            nav_context=nav_context,
            history=history,
            target_label=target_label,
        )
        _nav_print(f"指令: {plan.instruction} ({plan.rationale})",
                   page_hash=ph, round_num=round_num)

        # Reject repeated instructions
        if _is_repeated_instruction(plan.instruction, history):
            _nav_print("指令重复已失败操作，重试...", page_hash=ph, round_num=round_num)
            plan = _invoke_planner(
                initial_bytes, current_bytes,
                nav_context=nav_context,
                history=history,
                target_label=target_label,
            )
            if _is_repeated_instruction(plan.instruction, history):
                _nav_print("重试仍重复，放弃", page_hash=ph, round_num=round_num)
                log.append({"round": round_num, "instruction": plan.instruction,
                            "result": "重复指令放弃", "success": False})
                break

        # ── Step 3: Action Policy (1 LLM call) ──
        obs = Observation(png_bytes=current_bytes, source="planned_back_nav")
        decision = action_policy.decide(obs, plan.instruction)

        if decision.not_found_reason:
            _nav_print(f"目标不可见: {decision.not_found_reason}",
                       page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": f"元素不可见: {decision.not_found_reason}"})
            log.append({"round": round_num, "strategy": "planner",
                        "instruction": plan.instruction,
                        "result": f"元素不可见: {decision.not_found_reason}",
                        "success": False})
            continue

        action = decision.action

        # ── Step 4: Execute ──
        if action.action_type == "home":
            _nav_print("执行返回主屏", page_hash=ph, round_num=round_num)
            result = client.press_home()
            if "Failed" in result:
                from policy_expr.executor import WIN_W, WIN_H
                result = client.tap(WIN_W / 2, WIN_H - 16)
            tap_xy = None
        elif action.action_type in ("tap", "click") and action.x is not None and action.y is not None:
            ax, ay = action.x, action.y
            # YOLO calibration
            yolo_point = _yolo_detect_near(current_bytes, ax, ay)
            if yolo_point:
                _nav_print(f"YOLO校准: ({ax:.0f},{ay:.0f}) → ({yolo_point[0]:.0f},{yolo_point[1]:.0f})",
                           page_hash=ph, round_num=round_num)
                ax, ay = yolo_point
            if not is_valid_tap(ax, ay):
                _nav_print(f"坐标越界 ({ax:.0f},{ay:.0f})", page_hash=ph, round_num=round_num)
                history.append({"instruction": plan.instruction, "result": "坐标越界"})
                log.append({"round": round_num, "strategy": "planner",
                            "instruction": plan.instruction,
                            "result": "坐标越界", "success": False})
                continue
            lx, ly = logical_xy(ax, ay)
            tap_xy = (ax, ay)
            _nav_print(f"执行点击 ({lx:.0f},{ly:.0f})", page_hash=ph, round_num=round_num)
            result = client.tap(lx, ly)
        elif action.action_type == "scroll" and action.direction:
            _execute_scroll(action)
            tap_xy = (action.x or 500, action.y or 500)
            result = "scroll"
        else:
            _nav_print(f"不支持的动作: {action.action_type}", page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": f"不支持: {action.action_type}"})
            log.append({"round": round_num, "strategy": "planner",
                        "instruction": plan.instruction,
                        "result": f"不支持的动作: {action.action_type}",
                        "success": False})
            continue

        # Check tap failure
        if isinstance(result, str) and ("failed" in result.lower() or "interrupted" in result.lower()):
            _nav_print(f"执行失败: {result}", page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": f"执行失败: {result}"})
            log.append({"round": round_num, "strategy": "planner",
                        "instruction": plan.instruction,
                        "result": f"执行失败: {result}", "success": False})
            continue

        # ── Step 5: Verify page change ──
        time.sleep(BACK_SETTLE_SECONDS)
        after_bytes = screenshot()
        after_ph = _page_hash(after_bytes)

        # Check if page changed
        changed = False
        comp = _get_change_comp()
        unchanged, score = comp.no_change_score(current_bytes, after_bytes)
        if unchanged:
            diff_ratio = _pixel_diff_ratio(current_bytes, after_bytes)
            if diff_ratio < _PIXEL_DIFF_THRESHOLD:
                _nav_print(f"未变化 (pixel={diff_ratio:.4f})", page_hash=ph, round_num=round_num)
                consecutive_no_change += 1
                history.append({"instruction": plan.instruction, "result": "未变化"})
                log.append({
                    "round": round_num, "strategy": "planner",
                    "instruction": plan.instruction, "rationale": plan.rationale,
                    "result": "未变化", "success": False,
                    "page_from": ph, "page_to": after_ph,
                    **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
                })
                if consecutive_no_change >= _MAX_CONSECUTIVE_NO_CHANGE:
                    _nav_print("连续无变化，放弃", page_hash=ph, round_num=round_num)
                    break
                continue
            changed = True
        else:
            # Edge IoU reports changed — verify it's not dynamic content noise
            if id_comp.is_same_page(current_bytes, after_bytes).matched:
                _nav_print("动态内容误报", page_hash=ph, round_num=round_num)
                consecutive_no_change += 1
                history.append({"instruction": plan.instruction, "result": "动态内容未变化"})
                log.append({
                    "round": round_num, "strategy": "planner",
                    "instruction": plan.instruction,
                    "result": "动态内容未变化", "success": False,
                    "page_from": ph,
                })
                if consecutive_no_change >= _MAX_CONSECUTIVE_NO_CHANGE:
                    _nav_print("连续无变化，放弃", page_hash=ph, round_num=round_num)
                    break
                continue
            changed = True

        # Page changed
        consecutive_no_change = 0

        # Save after screenshot
        save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
        shot_str = save_if_changed(after_bytes, save_path)

        # Track page visits for loop detection
        visit_key = after_ph
        page_visit_counts[visit_key] = page_visit_counts.get(visit_key, 0) + 1
        if page_visit_counts[visit_key] >= _MAX_PAGE_VISITS:
            _nav_print(f"页面循环 ({visit_key} 访问{page_visit_counts[visit_key]}次)，放弃",
                       page_hash=ph, round_num=round_num)
            log.append({
                "round": round_num, "strategy": "planner",
                "instruction": plan.instruction, "rationale": plan.rationale,
                "result": f"页面循环 ({visit_key})", "success": False,
                "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            break

        # Check if we reached target or ancestor page
        matched_level = _match_stack(id_comp, nav_stack, after_bytes)
        if matched_level >= 0:
            is_initial = matched_level == top_level
            level_desc = "initial" if is_initial else f"L{matched_level}"
            sim = id_comp.raw_similarity(nav_stack[matched_level][0], after_bytes)
            _nav_print(f"→ 匹配 {level_desc} ({sim:.3f})", page_hash=ph, round_num=round_num)
            if status_cb:
                status_cb(f"← 回退 R{round_num} ✓ {'回到初始页' if is_initial else f'→ {level_desc}'}")
            log.append({
                "round": round_num, "strategy": "planner",
                "instruction": plan.instruction, "rationale": plan.rationale,
                "result": level_desc, "score": round(sim, 3), "success": is_initial,
                "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        # New unknown page — continue looping
        history.append({"instruction": plan.instruction, "result": "跳转到未知页"})
        log.append({
            "round": round_num, "strategy": "planner",
            "instruction": plan.instruction, "rationale": plan.rationale,
            "result": "未知页", "success": False,
            "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
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
    from policy_expr.executor import (
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
